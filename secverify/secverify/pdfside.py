"""Build a searchable reference corpus from the source PDF."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field

from .numbers import iter_tokens
from .textnorm import canonicalize

#: word fragments that can be glued into one figure when they touch
_NUMERIC_FRAGMENT = re.compile(r"^[\d,.()\-₹$%]+$")
#: max horizontal gap (pt) between fragments of the same letter-spaced figure;
#: real column gaps in financial tables are an order of magnitude wider
_MERGE_GAP = 1.5


@dataclass
class SearchText:
    """One canonical view of the PDF text with a map back to the raw pages."""

    canon: str = ""
    #: index_map[i] -> (page_index, offset_in_page_raw_text)
    index_map: list[tuple[int, int]] = field(default_factory=list)
    #: canonical offset at which each page starts
    page_starts: list[int] = field(default_factory=list)

    def page_of(self, canon_pos: int) -> int:
        """1-based page number for a canonical offset."""
        return bisect_right(self.page_starts, canon_pos)


@dataclass
class PdfCorpus:
    pages_raw: list[str] = field(default_factory=list)
    #: all alphanumeric characters — exact matching including figures
    alnum: SearchText = field(default_factory=SearchText)
    #: letters only — robust matching of labels/prose whose figures the PDF
    #: extraction interleaves differently (figures are validated separately)
    letters: SearchText = field(default_factory=SearchText)
    #: letters-only canonical text per page (for word-coverage checks)
    page_letters: list[str] = field(default_factory=list)
    #: canonical number key -> Counter of pages it appears on
    number_pages: dict[str, Counter] = field(default_factory=dict)
    #: canonical number key -> a raw token as it appeared in the PDF
    number_sample: dict[str, str] = field(default_factory=dict)
    number_counts: Counter = field(default_factory=Counter)

    def raw_snippet(
        self, view: SearchText, canon_start: int, canon_end: int, max_len: int = 400
    ) -> str:
        """Original PDF wording for a canonical range (clamped to one page)."""
        if not view.index_map:
            return ""
        canon_end = min(canon_end, len(view.index_map)) - 1
        if canon_end < canon_start:
            canon_end = canon_start
        page_a, off_a = view.index_map[canon_start]
        page_b, off_b = view.index_map[canon_end]
        if page_a != page_b:
            off_b = len(self.pages_raw[page_a]) - 1
        snippet = " ".join(self.pages_raw[page_a][off_a : off_b + 1].split())
        if len(snippet) > max_len:
            snippet = snippet[: max_len - 1] + "…"
        return snippet

    def has_number(self, key: str) -> bool:
        return key in self.number_counts

    def pages_for_number(self, key: str) -> list[int]:
        return sorted(self.number_pages.get(key, ()))

    def closest_numbers(self, key: str, limit: int = 3) -> list[str]:
        """PDF figures most likely to be what a mismatched HTML figure meant.

        Ranks by digit-string similarity (catches transpositions and typos),
        breaking ties by relative numeric distance.
        """
        from decimal import Decimal
        from difflib import SequenceMatcher

        try:
            target = Decimal(key)
        except Exception:
            return []
        scored = []
        for cand in self.number_counts:
            if cand == key:
                continue
            digit_sim = SequenceMatcher(None, key, cand, autojunk=False).ratio()
            try:
                cand_v = Decimal(cand)
                denom = max(abs(target), abs(cand_v), Decimal(1))
                num_dist = float(abs(cand_v - target) / denom)
            except Exception:
                num_dist = 1.0
            scored.append((-digit_sim, num_dist, cand))
        scored.sort()
        out = []
        for _sim, _dist, cand in scored[:limit]:
            pages = ", ".join(f"p.{p}" for p in self.pages_for_number(cand)[:3])
            out.append(f"{self.number_sample.get(cand, cand)} ({pages})")
        return out

    def _add_number(self, token: str, key: str, page_no: int) -> None:
        self.number_counts[key] += 1
        self.number_pages.setdefault(key, Counter())[page_no] += 1
        self.number_sample.setdefault(key, token)


def _repeated_lines(pages_text: list[str]) -> set[str]:
    """Detect running headers/footers: short lines on ≥40% of pages."""
    counts: Counter = Counter()
    for text in pages_text:
        seen = set()
        for line in text.splitlines():
            line = line.strip()
            if line and len(line) < 60:
                seen.add(line)
        counts.update(seen)
    threshold = max(2, int(len(pages_text) * 0.4))
    return {
        line
        for line, n in counts.items()
        if n >= threshold or (line.isdigit() and len(line) <= 3)
    }


def _merged_numeric_words(words: list[dict]) -> list[str]:
    """Glue letter-spaced figure fragments ("3 0 2 8" → "30", "28").

    Some PDF renderers letter-space table figures so each digit becomes its
    own word.  Fragments of one figure touch (gap ≈ 0), while neighbouring
    columns are tens of points apart, so a tiny gap threshold reassembles
    figures without ever bridging columns.
    """
    out: list[str] = []
    current = ""
    prev = None
    for w in words:
        is_frag = bool(_NUMERIC_FRAGMENT.match(w["text"]))
        same_line = prev is not None and abs(w["top"] - prev["top"]) < 2.0
        touching = prev is not None and (w["x0"] - prev["x1"]) <= _MERGE_GAP
        if is_frag and current and same_line and touching:
            current += w["text"]
        else:
            if current:
                out.append(current)
            current = w["text"] if is_frag else ""
        prev = w
    if current:
        out.append(current)
    return out


def load_pdf(path: str) -> PdfCorpus:
    import pdfplumber

    corpus = PdfCorpus()
    alnum_parts: list[str] = []
    letters_parts: list[str] = []

    with pdfplumber.open(path) as pdf:
        pages_text = [page.extract_text() or "" for page in pdf.pages]
        headers = _repeated_lines(pages_text)

        for page_idx, page in enumerate(pdf.pages):
            raw = pages_text[page_idx]

            # Numbers come from tightly-tokenised words so adjacent table
            # columns can never merge into one figure.  Pure-numeric tokens
            # go through the letter-spacing re-assembly; mixed tokens (like
            # "No.060408") are scanned as-is.  Each figure is counted exactly
            # once so occurrence counts can be compared against the HTML.
            words = page.extract_words(x_tolerance=1)
            mixed = [
                w["text"] for w in words if not _NUMERIC_FRAGMENT.match(w["text"])
            ]
            for text in mixed + _merged_numeric_words(words):
                for _s, _e, token, key in iter_tokens(text):
                    corpus._add_number(token, key, page_idx + 1)

            # The text corpus drops running headers/footers and bare page
            # numbers so sentences that span a page break still match.
            page_body = "\n".join(
                line for line in raw.splitlines() if line.strip() not in headers
            )
            corpus.pages_raw.append(page_body)

            for view, parts, letters_only in (
                (corpus.alnum, alnum_parts, False),
                (corpus.letters, letters_parts, True),
            ):
                view.page_starts.append(len(view.canon) + sum(map(len, parts)))
                canon, index_map = canonicalize(page_body, letters_only=letters_only)
                parts.append(canon)
                view.index_map.extend((page_idx, off) for off in index_map)

    corpus.alnum.canon = "".join(alnum_parts)
    corpus.letters.canon = "".join(letters_parts)
    corpus.page_letters = letters_parts
    return corpus
