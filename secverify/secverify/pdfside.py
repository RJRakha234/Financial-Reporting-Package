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
    #: display label per page — "p.5", or "auditorsreport p.2" when several
    #: reference PDFs are combined
    page_labels: list[str] = field(default_factory=list)
    #: canonical number key -> Counter of pages it appears on
    number_pages: dict[str, Counter] = field(default_factory=dict)
    #: canonical number key -> a raw token as it appeared in the PDF
    number_sample: dict[str, str] = field(default_factory=dict)
    number_counts: Counter = field(default_factory=Counter)
    #: canonical number key -> how many times it appeared NEGATIVE
    neg_counts: Counter = field(default_factory=Counter)
    #: page labels whose text extraction was suspiciously sparse
    #: (possible scanned / image content the tool cannot read)
    low_text_pages: list[str] = field(default_factory=list)

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

    def page_label(self, page_no: int) -> str:
        """Display label for a 1-based global page number."""
        if 1 <= page_no <= len(self.page_labels):
            return self.page_labels[page_no - 1]
        return f"p.{page_no}"

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
            pages = ", ".join(
                self.page_label(p) for p in self.pages_for_number(cand)[:3]
            )
            out.append(f"{self.number_sample.get(cand, cand)} ({pages})")
        return out

    def _add_number(self, token: str, key: str, page_no: int) -> None:
        from .numbers import token_attrs

        self.number_counts[key] += 1
        self.number_pages.setdefault(key, Counter())[page_no] += 1
        self.number_sample.setdefault(key, token)
        if token_attrs(token)[0] < 0:
            self.neg_counts[key] += 1

    def _remove_number(self, key: str, page_no: int) -> None:
        if self.number_counts.get(key, 0) <= 0:
            return
        self.number_counts[key] -= 1
        pages = self.number_pages.get(key)
        if pages and pages.get(page_no):
            pages[page_no] -= 1
            if not pages[page_no]:
                del pages[page_no]
        if self.number_counts[key] <= 0:
            del self.number_counts[key]
            self.number_pages.pop(key, None)
            self.number_sample.pop(key, None)


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


def load_pdf(path: str | list[str]) -> PdfCorpus:
    """Build one reference corpus from one or several PDFs.

    With several PDFs (e.g. the financial statements plus the signed
    auditor's report), pages are numbered globally but every reference in
    remarks carries a per-document label like ``auditorsreport p.2``.
    """
    import pdfplumber
    from pathlib import Path

    from .coverage import parse_index_line

    paths = [path] if isinstance(path, str) else list(path)
    multi = len(paths) > 1
    corpus = PdfCorpus()
    alnum_parts: list[str] = []
    letters_parts: list[str] = []
    global_idx = 0

    for doc_path in paths:
        stem = Path(doc_path).stem
        with pdfplumber.open(doc_path) as pdf:
            pages_text = [page.extract_text() or "" for page in pdf.pages]
            headers = _repeated_lines(pages_text)

            for doc_page_idx, page in enumerate(pdf.pages):
                raw = pages_text[doc_page_idx]
                page_no = global_idx + 1
                corpus.page_labels.append(
                    f"{stem} p.{doc_page_idx + 1}" if multi else f"p.{page_no}"
                )

                # A page that yields almost no text is probably scanned /
                # image-based — its content cannot be read or checked, so it
                # must be surfaced rather than silently skipped.
                if len(raw.strip()) < 40:
                    corpus.low_text_pages.append(
                        corpus.page_labels[-1]
                    )

                # Numbers come from tightly-tokenised words so adjacent table
                # columns can never merge into one figure.  Pure-numeric
                # tokens go through the letter-spacing re-assembly; mixed
                # tokens (like "No.060408") are scanned as-is.  Each figure
                # is counted exactly once so occurrence counts can be
                # compared against the HTML.
                words = page.extract_words(x_tolerance=1)
                mixed = [
                    w["text"]
                    for w in words
                    if not _NUMERIC_FRAGMENT.match(w["text"])
                ]
                for text in mixed + _merged_numeric_words(words):
                    for _s, _e, token, key in iter_tokens(text):
                        corpus._add_number(token, key, page_no)

                # Page numbers in a print index's dot-leader column are
                # print-only furniture (and often garbled by the leader
                # dots, 19 → "1.9"); remove them so they never count as
                # document figures.
                for line in raw.splitlines():
                    entry = parse_index_line(line)
                    if entry is None:
                        continue
                    for _s, _e, _token, key in iter_tokens(entry[1]):
                        corpus._remove_number(key, page_no)

                # The text corpus drops running headers/footers and bare
                # page numbers so sentences spanning a page break still
                # match.
                page_body = "\n".join(
                    line
                    for line in raw.splitlines()
                    if line.strip() not in headers
                )
                corpus.pages_raw.append(page_body)

                for view, parts, letters_only in (
                    (corpus.alnum, alnum_parts, False),
                    (corpus.letters, letters_parts, True),
                ):
                    view.page_starts.append(sum(map(len, parts)))
                    canon, index_map = canonicalize(
                        page_body, letters_only=letters_only
                    )
                    parts.append(canon)
                    view.index_map.extend((global_idx, off) for off in index_map)

                global_idx += 1

    corpus.alnum.canon = "".join(alnum_parts)
    corpus.letters.canon = "".join(letters_parts)
    corpus.page_letters = letters_parts
    return corpus


#: document-order anchors: minimum canonical (alnum) length for a line to
#: locate its document inside the HTML, and how many to sample per document
_DOCORDER_MIN_LEN = 40
_DOCORDER_MAX_ANCHORS = 80
_DOCORDER_MAX_PAGES = 8
#: a document needs at least this many distinctive, HTML-locatable anchors
#: before the tool will trust a position for it
_DOCORDER_MIN_FOUND = 3


def _order_docs_by_html(
    anchor_sets: list[list[str]], html_alnum: str
) -> list[int] | None:
    """Order of document indices by where their content sits in the HTML.

    Combined exhibits (e.g. interim condensed + annual statements, each with
    its auditor's report, in one HTML) must be checked in the HTML's own
    document order: the occurrence-pairing checks align the k-th PDF repeat
    of a label with the k-th HTML repeat, so a corpus concatenated in a
    different order would pair rows across the two financials.

    Anchors shared by two or more documents (identical rows repeated in both
    an annual and an interim statement) cannot place a document and are
    dropped.  Returns ``None`` when any document lacks enough distinctive,
    HTML-locatable anchors to be placed confidently — the caller keeps the
    given order in that case (the tool never guesses).
    """
    shared: Counter = Counter()
    for anchors in anchor_sets:
        shared.update(set(anchors))
    medians: list[int] = []
    for anchors in anchor_sets:
        found = sorted(
            pos
            for a in set(anchors)
            if shared[a] == 1 and (pos := html_alnum.find(a)) != -1
        )
        if len(found) < _DOCORDER_MIN_FOUND:
            return None
        medians.append(found[len(found) // 2])
    return sorted(range(len(anchor_sets)), key=lambda i: (medians[i], i))


def order_pdfs_to_html(
    paths: list[str], html_text: str
) -> tuple[list[str], bool]:
    """Reorder reference *paths* to match the HTML's document order.

    Samples distinctive lines (figures included, so an annual row and its
    interim twin stay distinguishable) from each PDF's first pages, locates
    them in the HTML, and sorts the PDFs by the median position.  Returns
    ``(ordered_paths, changed)``; on any doubt the given order is kept.
    """
    if len(paths) < 2:
        return list(paths), False
    import pdfplumber
    from bs4 import BeautifulSoup

    html_alnum, _ = canonicalize(
        BeautifulSoup(html_text, "html.parser").get_text(" ")
    )
    anchor_sets: list[list[str]] = []
    for p in paths:
        anchors: list[str] = []
        with pdfplumber.open(p) as pdf:
            for page in pdf.pages[:_DOCORDER_MAX_PAGES]:
                for line in (page.extract_text() or "").splitlines():
                    canon, _ = canonicalize(line)
                    if len(canon) >= _DOCORDER_MIN_LEN:
                        anchors.append(canon)
                if len(anchors) >= _DOCORDER_MAX_ANCHORS:
                    break
        anchor_sets.append(anchors)
    order = _order_docs_by_html(anchor_sets, html_alnum)
    if order is None or order == list(range(len(paths))):
        return list(paths), False
    return [paths[i] for i in order], True
