"""Canonical text form used for PDF↔HTML comparison.

PDF text extraction is unreliable about whitespace (words are sometimes run
together), and the two renderings disagree about punctuation (smart vs plain
quotes, en-dashes, non-breaking spaces, hyphenation at line breaks).  The
canonical form therefore keeps only alphanumeric characters, lower-cased,
with an index map back to the original string so matches can be shown in
their original wording.
"""

from __future__ import annotations

import re
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
        # Cheap overlap gate only — the padded window is longer than the
        # needle, so its raw ratio is diluted (a perfect substring of a
        # window 40 chars too long caps near 0.6).  The real decision is made
        # on the TRIMMED matched region below, against min_ratio.
        if sm.real_quick_ratio() < 0.3:
            continue
        blocks = sm.get_matching_blocks()[:-1]
        if not blocks:
            continue
        m_start = w_start + blocks[0].a
        m_end = w_start + blocks[-1].a + blocks[-1].size
        ratio = SequenceMatcher(
            None, corpus[m_start:m_end], needle, autojunk=False
        ).ratio()
        if ratio >= min_ratio and (best is None or ratio > best[2]):
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


#: how many lines at the top/bottom of a page count as header/footer territory
FURNITURE_EDGE_LINES = 3
#: a running header/footer must recur on at least this many pages
FURNITURE_MIN_PAGES = 3


def running_furniture(
    pages_raw: list[str],
    edge_lines: int = FURNITURE_EDGE_LINES,
    min_pages: int = FURNITURE_MIN_PAGES,
) -> set[str]:
    """Letters-only canonical forms of running page headers and footers.

    A print layout repeats a running header or footer in the top/bottom band of
    every page — ``"Infosys Limited - Press Release Page 3 of 8"``.  Its wording
    stays constant while its page number changes, so its **letters-only**
    canonical form recurs across pages while its full canonical form (digits
    included) differs every time.  That signature is what distinguishes it from a
    reprinted table column header, whose digits repeat *identically* and which
    the HTML does legitimately carry once.

    A web rendering has no pages and reproduces none of this furniture, so these
    lines must be excluded from the PDF-coverage check and from the number
    sequence.  Left in, each page contributes a phantom "content missing from the
    HTML" error and a phantom missing page number — noise that scales with the
    document's length and, being red, crowds out real findings.

    The varying digits must additionally be **page-number shaped** — bare 1-3
    digit integers, no thousands separator and no decimal part.  Without that
    condition a repeating data row whose figures change ("Schedule line 5 value
    5,000 5,500", printed at the top of successive pages) would match the same
    signature and be dropped as furniture, discarding real content.

    Returns the letters-only forms to skip.  Callers should additionally require
    the line to sit in the page's edge band, so a phrase that legitimately recurs
    in body text is never dropped.
    """
    groups: dict[str, tuple[set[int], set[str], bool]] = {}
    for page_idx, raw in enumerate(pages_raw):
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        n = len(lines)
        for pos, line in enumerate(lines):
            if not (pos < edge_lines or pos >= n - edge_lines):
                continue
            letters = canonical(line, letters_only=True)
            if len(letters) < 8:
                continue  # too short to identify a line safely
            pages, forms, page_shaped = groups.setdefault(
                letters, (set(), set(), True)
            )
            pages.add(page_idx)
            forms.add(canonical(line))
            nums = re.findall(r"\d[\d,]*(?:\.\d+)?", line)
            shaped = all(re.fullmatch(r"\d{1,3}", t) for t in nums)
            groups[letters] = (pages, forms, page_shaped and shaped)
    return {
        letters
        for letters, (pages, forms, page_shaped) in groups.items()
        if len(pages) >= min_pages and len(forms) > 1 and page_shaped
    }
