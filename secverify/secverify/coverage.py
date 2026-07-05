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


@dataclass
class CoverageLine:
    page: int
    text: str
    status: str  # "ok" | "review" | "missing"
    remark: str = ""
    #: review lines that indicate a probable omission (count shortfall) are
    #: escalated into the numbered issue list, not just the coverage map
    escalate: bool = False


@dataclass
class CoverageResult:
    lines: list[CoverageLine] = field(default_factory=list)
    total: int = 0
    ok: int = 0
    review: int = 0

    @property
    def missing(self) -> int:
        return self.total - self.ok - self.review


class HtmlCorpus:
    """Canonical views of the HTML's visible text, mirroring PdfCorpus."""

    def __init__(self, visible_text: str):
        from collections import Counter

        self.alnum = canonical(visible_text)
        self.letters = canonical(visible_text, letters_only=True)
        self.number_counts: Counter = Counter(
            key for _s, _e, _t, key in iter_tokens(visible_text)
        )
        self.number_keys = set(self.number_counts)


def _word_coverage(sentence: str, letters_corpus: str) -> float | None:
    words = [
        canonical(w, letters_only=True) for w in re.findall(r"[^\W\d_]+", sentence)
    ]
    words = [w for w in words if w]
    if len(words) < 2 or sum(len(w) for w in words) < 8:
        return None
    return sum(1 for w in words if w in letters_corpus) / len(words)


def _count_shortfall(
    needle: str, pdf_corpus: str, html_corpus: str
) -> tuple[int, int] | None:
    """(pdf_count, html_count) when *needle* occurs fewer times in the HTML.

    Catches an omission of content that also appears elsewhere in the
    document (e.g. a balance-sheet row whose label and figures repeat in a
    note) — presence checks alone cannot see one dropped instance of a
    repeated string.
    """
    if len(needle) < COUNT_CHECK_MIN_LEN:
        return None
    pdf_count = pdf_corpus.count(needle)
    if pdf_count < 2:
        return None  # presence checks already cover the single-instance case
    html_count = html_corpus.count(needle)
    if html_count < pdf_count:
        return (pdf_count, html_count)
    return None


def check_pdf_coverage(
    pages_raw: list[str],
    html: HtmlCorpus,
    pdf_alnum: str,
    pdf_letters: str,
) -> CoverageResult:
    result = CoverageResult()
    flagged_shortfalls: set[str] = set()  # report each distinct string once

    def shortfall_for(needle: str, pdf_corpus: str, html_corpus: str):
        if needle in flagged_shortfalls:
            return None
        shortfall = _count_shortfall(needle, pdf_corpus, html_corpus)
        if shortfall:
            flagged_shortfalls.add(needle)
        return shortfall

    for page_idx, raw in enumerate(pages_raw):
        for line in raw.splitlines():
            line = line.strip()
            c_alnum = canonical(line)
            if not c_alnum:
                continue
            result.total += 1
            page_no = page_idx + 1

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
                            f"This content appears {shortfall[0]}× in the PDF "
                            f"but only {shortfall[1]}× in the HTML — one "
                            "instance may have been dropped. Check every place "
                            "it should appear.",
                            escalate=True,
                        )
                    )
                    continue
                result.ok += 1
                result.lines.append(CoverageLine(page_no, line, "ok"))
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
                            f"This wording appears {shortfall[0]}× in the PDF "
                            f"but only {shortfall[1]}× in the HTML — one "
                            "instance may have been dropped. Check every place "
                            "it should appear.",
                            escalate=True,
                        )
                    )
                    continue
                result.ok += 1
                result.lines.append(CoverageLine(page_no, line, "ok"))
                continue

            if missing_figs:
                result.lines.append(
                    CoverageLine(
                        page_no,
                        line,
                        "missing",
                        "Figure(s) "
                        + ", ".join(f"“{t.strip()}”" for t in missing_figs)
                        + f" from PDF page {page_no} do not appear anywhere in "
                        "the HTML — a row or value may have been dropped or "
                        "mistyped.",
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
                    )
                )
                continue
            result.lines.append(
                CoverageLine(
                    page_no,
                    line,
                    "missing",
                    f"This PDF page {page_no} content was not found in the "
                    "HTML — it may have been omitted from the filing.",
                )
            )
    return result
