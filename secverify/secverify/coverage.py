"""Reverse direction: does the HTML reflect *everything* in the PDF?

The annotation pass colours the HTML by checking it against the PDF; that
alone cannot catch an **omission** — a paragraph or table row present in the
PDF but dropped from the HTML.  This pass walks every line of every PDF page
and checks it against the HTML text and figures, producing a page-by-page
coverage map rendered at the end of the review copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .numbers import is_significant, iter_tokens
from .textnorm import canonical, find_best_match

FUZZY_REVIEW_RATIO = 0.80
WORD_COVERAGE_RATIO = 0.85
#: minimum canonical length before occurrence counts are compared — short
#: strings ("total", "particulars") repeat too freely to count reliably
COUNT_CHECK_MIN_LEN = 12
#: a line repeated within this many lines of the top of the following page is
#: a table header reprinted after a page break, not a second occurrence
CONTINUATION_TOP_LINES = 3

#: an index / table-of-contents entry: label, dot leader, then a page number
#: (text extraction often garbles the number with leader dots: 19 → "1.9")
_INDEX_LINE_RE = re.compile(r"^(?P<label>.{3,}?)[.…]{4,}\s*(?P<pageno>[\d][\d.…\s]*)$")
#: column headers of an index's page-number column — print furniture only
_INDEX_FURNITURE = {"indexpageno", "pageno", "index", "contents", "tableofcontents"}


def parse_index_line(line: str) -> tuple[str, str] | None:
    """Split an index entry into ``(label, page_number_part)``, else None."""
    m = _INDEX_LINE_RE.match(line.strip())
    if not m:
        return None
    return m.group("label").strip(), m.group("pageno").strip()


@dataclass
class CoverageLine:
    page: int
    text: str
    status: str  # "ok" | "review" | "missing"
    remark: str = ""
    #: display label for the page ("p.5" or "auditorsreport p.2")
    label: str = ""
    #: review lines that indicate a probable omission (count shortfall) are
    #: escalated into the numbered issue list, not just the coverage map
    escalate: bool = False


@dataclass
class CoverageResult:
    lines: list[CoverageLine] = field(default_factory=list)
    total: int = 0
    ok: int = 0
    review: int = 0
    #: print-index entries whose page-number column was excluded from checks
    index_entries: int = 0

    @property
    def missing(self) -> int:
        return self.total - self.ok - self.review


class HtmlCorpus:
    """Canonical views of the HTML's visible text, mirroring PdfCorpus."""

    def __init__(self, visible_text: str):
        from collections import Counter

        from .textnorm import canonicalize

        self.visible_text = visible_text
        self.alnum = canonical(visible_text)
        self.letters, self._letters_map = canonicalize(
            visible_text, letters_only=True
        )
        self.number_counts: Counter = Counter(
            key for _s, _e, _t, key in iter_tokens(visible_text)
        )
        self.number_keys = set(self.number_counts)

    def letters_snippet(self, start: int, end: int, max_len: int = 200) -> str:
        """Original HTML wording for a letters-canonical range."""
        if not self._letters_map:
            return ""
        end = min(end, len(self._letters_map)) - 1
        if end < start:
            end = start
        raw = self.visible_text[self._letters_map[start] : self._letters_map[end] + 1]
        raw = " ".join(raw.split())
        return raw if len(raw) <= max_len else raw[: max_len - 1] + "…"


def _word_coverage(sentence: str, letters_corpus: str) -> float | None:
    words = [
        canonical(w, letters_only=True) for w in re.findall(r"[^\W\d_]+", sentence)
    ]
    words = [w for w in words if w]
    if len(words) < 2 or sum(len(w) for w in words) < 8:
        return None
    return sum(1 for w in words if w in letters_corpus) / len(words)


def _count_shortfall(
    needle: str,
    pdf_corpus: str,
    html_corpus: str,
    reprints: int = 0,
) -> tuple[int, int] | None:
    """(pdf_count, html_count) when *needle* occurs fewer times in the HTML.

    Catches an omission of content that also appears elsewhere in the
    document (e.g. a balance-sheet row whose label and figures repeat in a
    note) — presence checks alone cannot see one dropped instance of a
    repeated string.  *reprints* is the number of PDF occurrences that are
    page-continuation reprints (a table header reprinted after a page
    break); the HTML has no page breaks, so those are not real repeats and
    are deducted before comparing.
    """
    if len(needle) < COUNT_CHECK_MIN_LEN:
        return None
    pdf_count = pdf_corpus.count(needle) - reprints
    if pdf_count < 2:
        return None  # presence checks already cover the single-instance case
    html_count = html_corpus.count(needle)
    if html_count >= pdf_count:
        return None
    # A phrase occurring dozens of times (boilerplate embedded in longer
    # sentences) cannot be counted reliably — extraction quirks shift a
    # count by one or two.  Flag only few-occurrence content, or a
    # substantial relative shortfall.
    if pdf_count > 8 and (pdf_count - html_count) / pdf_count < 0.25:
        return None
    return (pdf_count, html_count)


def _shortfall_remark(
    shortfall: tuple[int, int],
    needle: str,
    page_canons: list[str],
    label_of,
    html: HtmlCorpus,
    letters_needle: str,
) -> str:
    """Explain a count shortfall in plain language, with locations and —
    when the HTML contains a near-variant — what the HTML says instead."""
    pdf_count, html_count = shortfall
    locs = [
        label_of(i + 1) for i, pc in enumerate(page_canons) if needle in pc
    ]
    diff = pdf_count - html_count
    remark = (
        f"The PDF contains this line {pdf_count}× (at "
        f"{', '.join(locs[:8]) or 'several places'}), but the HTML matches it "
        f"only {html_count}× — so {diff} occurrence"
        f"{'s are' if diff != 1 else ' is'} missing or worded differently in "
        "the HTML. Compare the HTML against each listed PDF location."
    )
    if letters_needle and len(letters_needle) >= COUNT_CHECK_MIN_LEN:
        masked = html.letters.replace(
            letters_needle, "\x00" * len(letters_needle)
        )
        variant = find_best_match(masked, letters_needle, min_ratio=0.70)
        if variant is not None:
            s, e, ratio = variant
            snippet = html.letters_snippet(s, e)
            if snippet:
                remark += (
                    f" Likely cause: in at least one place the HTML instead "
                    f"says “{snippet}” (similarity {ratio:.0%})."
                )
    return remark


def _continuation_reprints(pages_raw: list[str]) -> "Counter":
    """Count page-continuation header reprints per canonical line.

    A print layout reprints a table's column-header row at the top of the
    next page when the table spans a page break.  Such a line — appearing in
    the first few lines of a page AND also present on the previous page —
    is one logical occurrence, not two.
    """
    from collections import Counter

    reprints: Counter = Counter()
    prev_lines: set[str] = set()
    for raw in pages_raw:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        canon_lines = [canonical(ln) for ln in lines]
        for pos, c in enumerate(canon_lines):
            if pos < CONTINUATION_TOP_LINES and c and c in prev_lines:
                reprints[c] += 1
                reprints[canonical(lines[pos], letters_only=True)] += 1
        prev_lines = {c for c in canon_lines if c}
    return reprints


def check_pdf_coverage(
    pages_raw: list[str],
    html: HtmlCorpus,
    pdf_alnum: str,
    pdf_letters: str,
    page_labels: list[str] | None = None,
) -> CoverageResult:
    def label_of(page_no: int) -> str:
        if page_labels and 1 <= page_no <= len(page_labels):
            return page_labels[page_no - 1]
        return f"p.{page_no}"
    result = CoverageResult()
    flagged_shortfalls: set[str] = set()  # report each distinct string once
    reprints = _continuation_reprints(pages_raw)
    page_alnums = [canonical(raw) for raw in pages_raw]
    page_letts = [canonical(raw, letters_only=True) for raw in pages_raw]

    def shortfall_for(needle: str, pdf_corpus: str, html_corpus: str):
        if needle in flagged_shortfalls:
            return None
        shortfall = _count_shortfall(
            needle, pdf_corpus, html_corpus, reprints.get(needle, 0)
        )
        if shortfall:
            flagged_shortfalls.add(needle)
        return shortfall

    # Detect table-of-contents pages so index furniture can be skipped.
    index_pages = {
        page_idx
        for page_idx, raw in enumerate(pages_raw)
        if sum(1 for ln in raw.splitlines() if parse_index_line(ln)) >= 3
    }

    for page_idx, raw in enumerate(pages_raw):
        for line in raw.splitlines():
            line = line.strip()

            index_entry = parse_index_line(line)
            if index_entry:
                # A print index entry: validate the label; the page-number
                # column is print-only (and often garbled by dot leaders),
                # so it is not expected in an unpaginated HTML.
                line = index_entry[0]
                result.index_entries += 1
            elif (
                page_idx in index_pages
                and canonical(line, letters_only=True) in _INDEX_FURNITURE
            ):
                # e.g. the "Index  Page No." column header itself.
                result.index_entries += 1
                continue

            c_alnum = canonical(line)
            if not c_alnum:
                continue
            result.total += 1
            page_no = page_idx + 1
            page_label = label_of(page_no)

            # Tier 1: the whole line, figures included, appears verbatim.
            if c_alnum in html.alnum:
                shortfall = shortfall_for(c_alnum, pdf_alnum, html.alnum)
                if shortfall:
                    result.review += 1
                    result.lines.append(
                        CoverageLine(
                            page_no,
                            line,
                            "review",
                            _shortfall_remark(
                                shortfall, c_alnum, page_alnums, label_of,
                                html, canonical(line, letters_only=True),
                            ),
                            escalate=True,
                            label=page_label,
                        )
                    )
                    continue
                result.ok += 1
                result.lines.append(CoverageLine(page_no, line, "ok", label=page_label))
                continue

            letters = canonical(line, letters_only=True)
            text_ok = not letters or letters in html.letters
            missing_figs = [
                token
                for _s, _e, token, key in iter_tokens(line)
                if key not in html.number_keys and is_significant(key, token)
            ]

            if text_ok and not missing_figs:
                # Words match contiguously and every significant figure is in
                # the HTML — but if this wording repeats, make sure the HTML
                # repeats it just as often.
                shortfall = shortfall_for(letters, pdf_letters, html.letters)
                if shortfall:
                    result.review += 1
                    result.lines.append(
                        CoverageLine(
                            page_no,
                            line,
                            "review",
                            _shortfall_remark(
                                shortfall, letters, page_letts, label_of,
                                html, letters,
                            ),
                            escalate=True,
                            label=page_label,
                        )
                    )
                    continue
                result.ok += 1
                result.lines.append(CoverageLine(page_no, line, "ok", label=page_label))
                continue

            if missing_figs:
                result.lines.append(
                    CoverageLine(
                        page_no,
                        line,
                        "missing",
                        "Figure(s) "
                        + ", ".join(f"“{t.strip()}”" for t in missing_figs)
                        + f" from PDF {page_label} do not appear anywhere in "
                        "the HTML — a row or value may have been dropped or "
                        "mistyped.",
                        label=page_label,
                    )
                )
                continue

            # Words are not contiguous in the HTML: fuzzy, then word coverage.
            match = find_best_match(html.letters, letters)
            if match is not None and match[2] >= FUZZY_REVIEW_RATIO:
                result.review += 1
                result.lines.append(
                    CoverageLine(
                        page_no,
                        line,
                        "review",
                        f"Close match in the HTML (similarity {match[2]:.0%}) "
                        "but not identical — verify the wording.",
                        label=page_label,
                    )
                )
                continue
            coverage = _word_coverage(line, html.letters)
            if coverage is not None and coverage >= WORD_COVERAGE_RATIO:
                result.review += 1
                result.lines.append(
                    CoverageLine(
                        page_no,
                        line,
                        "review",
                        f"{coverage:.0%} of the words appear in the HTML but "
                        "not contiguously — usually a table whose reading "
                        "order differs between the two renderings. Verify "
                        "manually.",
                        label=page_label,
                    )
                )
                continue
            result.lines.append(
                CoverageLine(
                    page_no,
                    line,
                    "missing",
                    f"This PDF {page_label} content was not found in the "
                    "HTML — it may have been omitted from the filing.",
                    label=page_label,
                )
            )
    return result
