"""Canonical text form used for PDF↔HTML comparison.

PDF text extraction is unreliable about whitespace (words are sometimes run
together), and the two renderings disagree about punctuation (smart vs plain
quotes, en-dashes, non-breaking spaces, hyphenation at line breaks).  The
canonical form therefore keeps only alphanumeric characters, lower-cased,
with an index map back to the original string so matches can be shown in
their original wording.
"""

from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher


def canonicalize(text: str, letters_only: bool = False) -> tuple[str, list[int]]:
    """Return ``(canonical, index_map)``.

    ``index_map[i]`` is the offset in *text* of the character that produced
    ``canonical[i]``.  Unicode is NFKD-decomposed per character so ligatures
    (ﬁ → fi) expand while offsets stay correct.  With ``letters_only`` digits
    are dropped too — used to match labels/prose independently of the figures
    interleaved into them.
    """
    keep = str.isalpha if letters_only else str.isalnum
    out: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        for part in unicodedata.normalize("NFKD", ch):
            if keep(part):
                out.append(part.lower())
                index_map.append(i)
    return "".join(out), index_map


def canonical(text: str, letters_only: bool = False) -> str:
    return canonicalize(text, letters_only=letters_only)[0]


def find_best_match(
    corpus: str, needle: str, min_ratio: float = 0.60
) -> tuple[int, int, float] | None:
    """Locate the region of *corpus* most similar to *needle*.

    Returns ``(start, end, ratio)`` in corpus coordinates, or None.  Instead
    of running SequenceMatcher over the whole corpus (quadratic), short
    "shingles" sampled from the needle anchor candidate windows which are
    then scored locally.
    """
    n = len(needle)
    if n < 12 or not corpus:
        return None

    shingle_len = 12
    positions = {0, n // 4, n // 2, (3 * n) // 4, n - shingle_len}
    candidates: set[int] = set()
    for pos in positions:
        shingle = needle[pos : pos + shingle_len]
        start = 0
        hits = 0
        while hits < 25:
            found = corpus.find(shingle, start)
            if found == -1:
                break
            candidates.add(max(0, found - pos))
            start = found + 1
            hits += 1

    best: tuple[int, int, float] | None = None
    pad = max(20, n // 5)
    for cand in candidates:
        w_start = max(0, cand - pad)
        w_end = min(len(corpus), cand + n + pad)
        window = corpus[w_start:w_end]
        sm = SequenceMatcher(None, window, needle, autojunk=False)
        if sm.real_quick_ratio() < min_ratio or sm.quick_ratio() < min_ratio:
            continue
        if sm.ratio() < min_ratio:
            continue
        # Trim the window to the matched region and re-score there, so the
        # padding does not dilute the reported similarity.
        blocks = sm.get_matching_blocks()[:-1]
        if blocks:
            m_start = w_start + blocks[0].a
            m_end = w_start + blocks[-1].a + blocks[-1].size
        else:
            m_start, m_end = w_start, w_end
        ratio = SequenceMatcher(
            None, corpus[m_start:m_end], needle, autojunk=False
        ).ratio()
        if best is None or ratio > best[2]:
            best = (m_start, m_end, ratio)
    return best


def word_subsequence_span(
    haystack: str, words: list[str], start: int = 0
) -> tuple[int, int] | None:
    """Earliest span of *haystack* containing *words* in order (gaps allowed).

    Used to recognise a table label whose words the PDF layout interleaves
    with other columns' text: the label's own words still appear in their
    original order, just with junk in between.
    """
    pos = start
    first = None
    for word in words:
        i = haystack.find(word, pos)
        if i == -1:
            return None
        if first is None:
            first = i
        pos = i + len(word)
    return (first, pos) if first is not None else None


def smallest_subsequence_window(haystack: str, words: list[str]) -> int | None:
    """Length of the tightest in-order window for *words*, or None."""
    if not words:
        return None
    best: int | None = None
    start = 0
    while True:
        span = word_subsequence_span(haystack, words, start)
        if span is None:
            return best
        length = span[1] - span[0]
        if best is None or length < best:
            best = length
        start = span[0] + 1


def split_sentences(text: str) -> list[str]:
    """Split prose into sentence-ish chunks for granular matching."""
    import re

    parts = re.split(r"(?<=[.!?;:])\s+", text)
    return [p for p in (part.strip() for part in parts) if p]
