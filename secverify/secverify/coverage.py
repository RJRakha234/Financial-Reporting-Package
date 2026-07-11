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
#: a duplication is only flagged for a distinctive line this long (letters).
#: Statement titles, the "for and on behalf of the Board" signature line and
#: accounting-standard names legitimately recur across an exhibit and run up to
#: ~80 chars; requiring more keeps the check false-positive-free (verified zero
#: on the reference filings) while still catching a pasted note paragraph.
DUP_CHECK_MIN_LEN = 80
#: a line repeated within this many lines of the top of the following page is
#: a table header reprinted after a page break, not a second occurrence
CONTINUATION_TOP_LINES = 3

#: sequence anchors must be this distinctive (letters) to vote on ordering
ORDER_ANCHOR_MIN_LEN = 25
#: displacement below this many letters-canon characters is table/cell
#: jitter, not a moved section
ORDER_SLACK = 400
#: a prose anchor out of sequence by at least this many letters-canon chars
#: (roughly one sentence) is reported for review even inside ORDER_SLACK —
#: a relocated paragraph whose every word is verbatim would otherwise be the
#: one silent way to change a document's reading order
ORDER_SOFT_MIN = 40
#: only digit-light lines (prose, not table rows) join the soft check, so
#: print-vs-web cell jitter cannot flood it
ORDER_SOFT_MAX_DIGIT_FRAC = 0.15

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
    #: issue kind for the panel: "omission" | "sign" | "column-order" |
    #: "currency" | "row-value" | "duplicate"
    issue_kind: str = "omission"


@dataclass
class OrderIssue:
    """A run of PDF lines whose HTML position breaks the PDF sequence."""

    pdf_label: str      # page label of the first misplaced line
    first_text: str
    last_text: str
    count: int          # lines in the run
    direction: str      # "earlier" | "later"
    near_label: str     # PDF page whose content surrounds it in the HTML
    #: True for a small relocation (within the red check's slack) — a prose
    #: line out of sequence by roughly a paragraph; review, not error
    review: bool = False


@dataclass
class CoverageResult:
    lines: list[CoverageLine] = field(default_factory=list)
    total: int = 0
    ok: int = 0
    review: int = 0
    #: distinctive PDF lines reproduced MORE times in the HTML than the PDF
    #: has them (content pasted twice during conversion)
    duplications: list[CoverageLine] = field(default_factory=list)
    #: print-index entries whose page-number column was excluded from checks
    index_entries: int = 0
    #: runs of content that appear out of sequence in the HTML
    order_issues: list[OrderIssue] = field(default_factory=list)
    #: value-integrity coverage — figure-bearing rows fully value-checked vs
    #: skipped (with the reason), so the report can state its own scope
    rows_with_figures: int = 0
    rows_value_checked: int = 0
    rows_value_skipped: "Counter" = field(default_factory=lambda: __import__(
        "collections").Counter())
    #: page labels whose PDF text was too sparse to read (possible scans)
    low_text_pages: list[str] = field(default_factory=list)

    @property
    def missing(self) -> int:
        return self.total - self.ok - self.review


class HtmlCorpus:
    """Canonical views of the HTML's visible text, mirroring PdfCorpus."""

    def __init__(self, visible_text: str, block_texts: "list[str] | None" = None):
        from collections import Counter

        from .textnorm import canonicalize

        self.visible_text = visible_text
        self.alnum = canonical(visible_text)
        self.letters, self._letters_map = canonicalize(
            visible_text, letters_only=True
        )
        from .numbers import token_attrs

        self.number_counts: Counter = Counter()
        self.neg_counts: Counter = Counter()
        for _s, _e, tok, key in iter_tokens(visible_text):
            self.number_counts[key] += 1
            if token_attrs(tok)[0] < 0:
                self.neg_counts[key] += 1
        self.number_keys = set(self.number_counts)

        # Whole-block occurrence counts.  Counting a phrase by substring in
        # one concatenated blob is unreliable — it misses boundaries and
        # counts a phrase embedded inside a longer cell.  Counting exact
        # block (cell/paragraph) matches respects boundaries, so a heading
        # that merely starts with the phrase is not miscounted as a repeat.
        self.seg_alnum: Counter = Counter()
        self.seg_letters: Counter = Counter()
        self.seg_sample: dict[str, str] = {}
        for bt in block_texts or []:
            ca = canonical(bt)
            if ca:
                self.seg_alnum[ca] += 1
            cl = canonical(bt, letters_only=True)
            if cl:
                self.seg_letters[cl] += 1
                self.seg_sample.setdefault(cl, bt.strip())

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
    pdf_count: int,
    html_count: int,
    reprints: int = 0,
) -> tuple[int, int] | None:
    """(pdf_count, html_count) when *needle* occurs fewer times in the HTML.

    Counts are **whole-line / whole-block** occurrences (not substrings), so
    a phrase embedded in a longer cell or reused as a heading prefix is not
    miscounted.  Catches an omission of content that also appears elsewhere
    (a balance-sheet row repeated in a note).  *reprints* is the number of
    PDF occurrences that are page-continuation reprints (a header reprinted
    after a page break); the HTML has no page breaks, so those are deducted.
    """
    if len(needle) < COUNT_CHECK_MIN_LEN:
        return None
    pdf_count -= reprints
    if pdf_count < 2:
        return None  # presence checks already cover the single-instance case
    if html_count >= pdf_count:
        return None
    # Very common boilerplate: a one-off count wobble is noise, so require a
    # substantial relative shortfall before flagging.
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


def _rich_figs(
    text: str, significant_only: bool = False
) -> list[tuple[str, int, str, bool]]:
    """Ordered ``(magnitude_key, sign, currency, is_percent)`` for *text*.

    With *significant_only*, footnote markers like ``(1)`` and note
    references (bare integers < 100, year-like values) are dropped so they
    cannot masquerade as negative figures in the row comparison.
    """
    from .numbers import token_attrs

    out = []
    for _s, _e, token, key in iter_tokens(text):
        if significant_only and not is_significant(key, token):
            continue
        # A leading zero marks an identifier (membership/UDIN/registration
        # number), never a monetary amount.
        if re.match(r"^\(?0\d", token.strip()):
            continue
        sign, currency, pct = token_attrs(token)
        out.append((key, sign, currency, pct))
    return out


def _subseq_indices(hay: list[str], needle: list[str]) -> list[int] | None:
    """Indices of the first in-order occurrence of *needle* within *hay*."""
    idx = []
    pos = 0
    for want in needle:
        while pos < len(hay) and hay[pos] != want:
            pos += 1
        if pos == len(hay):
            return None
        idx.append(pos)
        pos += 1
    return idx


def _compare_row(
    row_figs: list[tuple[str, int, str, bool]],
    win_figs: list[tuple[str, int, str, bool]],
) -> tuple[str, str]:
    """Classify how a row's figures differ between PDF and an HTML window.

    Returns ``(verdict, detail)`` where verdict is one of ``ok`` /
    ``row-value`` (a magnitude missing → likely swapped/wrong value) /
    ``column-order`` (all magnitudes present but in a different left-to-right
    order → comparative columns transposed) / ``sign`` (a negative shown
    positive or vice versa) / ``currency`` (₹↔$) / ``percent`` (a % gained
    or lost).
    """
    from collections import Counter

    row_mag = [f[0] for f in row_figs]
    win_mag = [f[0] for f in win_figs]
    if not (Counter(row_mag) <= Counter(win_mag)):
        return "row-value", ""
    idx = _subseq_indices(win_mag, row_mag)
    if idx is None:
        return "column-order", ""
    for r, wi in zip(row_figs, idx):
        w = win_figs[wi]
        # Sign is verified document-wide by the sign census (independent of
        # row-label length), so it is not re-checked here.
        if r[2] and w[2] and r[2] != w[2]:
            # Only a genuine symbol swap (₹↔$); a symbol present on one side
            # and absent on the other is normal (the unit sits in a header).
            return "currency", f"figure {r[0]}: PDF “{r[2]}”, HTML “{w[2]}”"
        if r[3] != w[3]:
            return "percent", (
                f"figure {r[0]}: PDF {'%' if r[3] else 'plain'}, "
                f"HTML {'%' if w[3] else 'plain'}"
            )
    return "ok", ""


#: severity order for choosing which occurrence's verdict to report
_ROW_SEVERITY = {"ok": 0, "sign": 1, "currency": 2, "percent": 3,
                 "column-order": 4, "row-value": 5}


def _row_figures_near_label(
    label_letters: str,
    row_figs: list[tuple[str, int, str, bool]],
    html: HtmlCorpus,
    line_len: int,
    next_letters: str,
    occ_index: int,
) -> tuple[str, str, str]:
    """Do the row's figures sit — same values, order, sign, currency — beside
    the RIGHT occurrence of its label?

    Presence checks alone accept two line items whose figures were swapped,
    period columns transposed, or a negative shown positive: every value
    still exists somewhere.  Both documents present content in the same
    order (the content-order check enforces this), so the *occ_index*-th PDF
    occurrence of the label is examined against the corresponding HTML
    occurrence (±1 to tolerate a wording variation elsewhere).  The window
    is truncated at the next PDF row's label so an adjacent row cannot lend
    its figures.  Returns ``(verdict, detail, html_snippet)``.
    """
    from .textnorm import canonicalize

    window_len = max(240, line_len * 2)
    positions: list[int] = []
    pos = html.letters.find(label_letters)
    while pos != -1 and len(positions) < occ_index + 2:
        positions.append(pos)
        pos = html.letters.find(label_letters, pos + 1)
    candidates = [
        p for k, p in enumerate(positions) if occ_index - 1 <= k <= occ_index + 1
    ]
    if not candidates:
        return "ok", "", ""  # cannot locate: count checks report the shortfall

    best = ("row-value", "", "")
    for cand in candidates:
        raw_start = html._letters_map[cand]
        window = html.visible_text[raw_start : raw_start + window_len]
        if next_letters:
            wl, wmap = canonicalize(window, letters_only=True)
            cut = wl.find(next_letters, len(label_letters))
            if cut != -1 and wmap:
                label_end_raw = wmap[min(len(label_letters), len(wmap)) - 1]
                raw_cut = wmap[cut]
                between = window[label_end_raw + 1 : raw_cut]
                if not any(ch.isdigit() for ch in between):
                    # The "next row's label" follows immediately with no
                    # figures in between: this PDF line is a label wrapped
                    # across lines, not a complete row — cannot assess.
                    return "ok", "", ""
                window = window[:raw_cut]
        win_figs = _rich_figs(window)
        verdict, detail = _compare_row(row_figs, win_figs)
        if verdict == "ok":
            return "ok", "", ""
        if _ROW_SEVERITY[verdict] < _ROW_SEVERITY[best[0]]:
            best = (verdict, detail, " ".join(window.split())[:220])
    return best


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


def _lis_indices(values: list[int]) -> set[int]:
    """Indices forming a longest strictly-increasing subsequence."""
    if not values:
        return set()
    from bisect import bisect_left

    tails: list[int] = []          # last value of LIS of each length
    tails_idx: list[int] = []      # index of that value
    prev = [-1] * len(values)
    for i, v in enumerate(values):
        j = bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[j] = v
            tails_idx[j] = i
        prev[i] = tails_idx[j - 1] if j > 0 else -1
    out: set[int] = set()
    i = tails_idx[-1]
    while i != -1:
        out.add(i)
        i = prev[i]
    return out


def _order_issues(
    anchors: list[tuple[int, str, str, int]],
    label_of,
    lines: list[CoverageLine],
) -> list[OrderIssue]:
    """Detect PDF content that the HTML presents out of sequence.

    *anchors* are ``(line_idx, doc, label, html_pos)`` for PDF lines that
    occur exactly once in the HTML — reliable sequence markers.  Within each
    source document their HTML positions must increase; anchors outside the
    longest increasing subsequence, displaced by more than ORDER_SLACK, are
    misplaced content.  Consecutive misplaced anchors merge into one issue.
    """
    issues: list[OrderIssue] = []
    by_doc: dict[str, list[tuple[int, str, int]]] = {}
    for line_idx, doc, label, pos in anchors:
        by_doc.setdefault(doc, []).append((line_idx, label, pos))

    def _digit_frac(text: str) -> float:
        chars = [c for c in text if c.isalnum()]
        if not chars:
            return 1.0
        return sum(c.isdigit() for c in chars) / len(chars)

    for doc_anchors in by_doc.values():
        positions = [pos for _i, _l, pos in doc_anchors]
        keep = _lis_indices(positions)
        violators: list[int] = []
        soft: list[int] = []
        for a_idx in range(len(doc_anchors)):
            if a_idx in keep:
                continue
            pos = positions[a_idx]
            lo = max((positions[k] for k in keep if k < a_idx), default=None)
            hi = min((positions[k] for k in keep if k > a_idx), default=None)
            if (lo is None or pos >= lo - ORDER_SLACK) and (
                hi is None or pos <= hi + ORDER_SLACK
            ):
                # Within the red check's slack — but a digit-light PROSE line
                # out of sequence by a sentence-plus is a relocated paragraph
                # (every word verbatim, just in the wrong place): review it.
                displaced = 0
                if lo is not None and pos < lo:
                    displaced = lo - pos
                if hi is not None and pos > hi:
                    displaced = max(displaced, pos - hi)
                line_idx = doc_anchors[a_idx][0]
                if (
                    displaced >= ORDER_SOFT_MIN
                    and _digit_frac(lines[line_idx].text)
                    <= ORDER_SOFT_MAX_DIGIT_FRAC
                ):
                    soft.append(a_idx)
                continue
            violators.append(a_idx)

        def emit_runs(idxs: list[int], review: bool) -> None:
            run: list[int] = []
            for a_idx in [*idxs, None]:
                if run and (a_idx is None or a_idx != run[-1] + 1):
                    first_i, first_label, first_pos = doc_anchors[run[0]]
                    last_i, _l, _p = doc_anchors[run[-1]]
                    lo = max(
                        (positions[k] for k in keep if k < run[0]), default=None
                    )
                    direction = (
                        "earlier" if lo is not None and first_pos < lo else "later"
                    )
                    near = min(
                        (doc_anchors[k] for k in keep),
                        key=lambda a: abs(a[2] - first_pos),
                        default=None,
                    )
                    issues.append(
                        OrderIssue(
                            pdf_label=first_label,
                            first_text=lines[first_i].text,
                            last_text=lines[last_i].text,
                            count=len(run),
                            direction=direction,
                            near_label=near[1] if near else "?",
                            review=review,
                        )
                    )
                    run = []
                if a_idx is not None:
                    run.append(a_idx)

        emit_runs(violators, review=False)
        emit_runs(soft, review=True)
    return issues


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
    #: (line_idx, doc, label, html_pos) for lines occurring exactly once in
    #: the HTML — sequence markers for the content-order check
    anchors: list[tuple[int, str, str, int]] = []

    def collect_anchor(line: str, page_label: str) -> None:
        letters_line = canonical(line, letters_only=True)
        if len(letters_line) < ORDER_ANCHOR_MIN_LEN:
            return
        pos = html.letters.find(letters_line)
        if pos == -1 or html.letters.find(letters_line, pos + 1) != -1:
            return  # absent or ambiguous in the HTML: cannot vote on ordering
        if pdf_letters.count(letters_line) != 1:
            # Repeated in the PDF (e.g. a table header reprinted after a
            # page break): its single HTML occurrence cannot say which PDF
            # occurrence it reflects.
            return
        doc = page_label.rsplit(" p.", 1)[0] if " p." in page_label else ""
        anchors.append((len(result.lines) - 1, doc, page_label, pos))

    # Section / note reference numbers ("1.1", "2.15", "2.11.1") are outline
    # identifiers, not monetary figures — collect them so the row value check
    # never treats them as amounts.
    _section_re = re.compile(r"^\s*(\d+(?:\.\d+)+)")
    section_ref_keys: set[str] = set()
    for raw in pages_raw:
        for ln in raw.splitlines():
            m = _section_re.match(ln)
            src = m.group(1) if m else None
            ie = parse_index_line(ln)
            if ie:
                m2 = _section_re.match(ie[0])
                if m2:
                    src = m2.group(1)
            if src:
                for _s, _e, _t, k in iter_tokens(src):
                    section_ref_keys.add(k)

    # A label that is a PREFIX of a longer line's label is ambiguous: string
    # search would pair it with the longer sibling (e.g. "Total comprehensive
    # income" vs "Total comprehensive income for the period" in the statement
    # of changes in equity).  Such labels are excluded from the placement,
    # duplicate and count checks so a mispairing cannot raise a false alarm.
    _all_line_letters = sorted(
        {
            canonical(ln, letters_only=True)
            for raw in pages_raw
            for ln in raw.splitlines()
            if len(canonical(ln, letters_only=True)) >= 8
        }
    )

    def prefix_ambiguous(letters_line: str) -> bool:
        from bisect import bisect_right

        i = bisect_right(_all_line_letters, letters_line)
        return (
            i < len(_all_line_letters)
            and _all_line_letters[i].startswith(letters_line)
            and _all_line_letters[i] != letters_line
        )

    flagged_shortfalls: set[str] = set()  # report each distinct string once
    reprints = _continuation_reprints(pages_raw)
    page_alnums = [canonical(raw) for raw in pages_raw]
    page_letts = [canonical(raw, letters_only=True) for raw in pages_raw]
    page_letts_starts: list[int] = []
    _acc = 0
    for _pl in page_letts:
        page_letts_starts.append(_acc)
        _acc += len(_pl)

    # Whole-line PDF occurrence counts (mirrors the HTML block counts), so
    # counting respects line/cell boundaries instead of counting substrings.
    from collections import Counter as _Counter

    pdf_seg_alnum: _Counter = _Counter()
    pdf_seg_letters: _Counter = _Counter()
    for raw in pages_raw:
        for ln in raw.splitlines():
            ca = canonical(ln)
            if ca:
                pdf_seg_alnum[ca] += 1
            cl = canonical(ln, letters_only=True)
            if cl:
                pdf_seg_letters[cl] += 1

    def shortfall_for(needle: str, pdf_seg: "_Counter", html_seg: "_Counter"):
        if needle in flagged_shortfalls:
            return None
        shortfall = _count_shortfall(
            needle, pdf_seg.get(needle, 0), html_seg.get(needle, 0),
            reprints.get(needle, 0),
        )
        if shortfall:
            flagged_shortfalls.add(needle)
        return shortfall

    # Duplication — the reverse of the shortfall check: a distinctive line that
    # occurs exactly once in the PDF but two or more times in the HTML (a note
    # or row pasted twice during conversion). Gated hard (long line, PDF count
    # exactly one) so legitimately repeated content never false-flags.
    for cl, hcount in html.seg_letters.items():
        if len(cl) < DUP_CHECK_MIN_LEN or hcount < 2:
            continue
        if pdf_seg_letters.get(cl, 0) == 1:
            sample = html.seg_sample.get(cl, "")
            result.duplications.append(
                CoverageLine(
                    0, sample, "review",
                    remark=(
                        f"Duplicated content — this line appears once in the "
                        f"PDF but {hcount} times in the HTML. A note or row may "
                        "have been pasted twice during conversion; remove the "
                        "extra copy or confirm the repetition is intended."
                    ),
                    escalate=True, issue_kind="duplicate",
                )
            )

    # Detect table-of-contents pages so index furniture can be skipped.
    index_pages = {
        page_idx
        for page_idx, raw in enumerate(pages_raw)
        if sum(1 for ln in raw.splitlines() if parse_index_line(ln)) >= 3
    }

    for page_idx, raw in enumerate(pages_raw):
        page_lines = [ln.strip() for ln in raw.splitlines()]
        page_letters_offset = 0
        for line_no, line in enumerate(page_lines):
            line_letters_offset = page_letters_offset
            page_letters_offset += len(canonical(line, letters_only=True))

            # Strip a leading run of 1-3 identical capital letters that some
            # PDFs prepend to headings as invisible navigation anchors
            # ("XINFOSYS LIMITED", "XXX2.1 BUSINESS", "X2.11 EQUITY") — only
            # when doing so makes the line match the HTML, so real content is
            # never altered.
            m_anchor = re.match(r"^([A-Z])\1{0,2}(?=[A-Z0-9])", line)
            if m_anchor:
                stripped = line[m_anchor.end():]
                if canonical(stripped) in html.alnum or canonical(
                    stripped, letters_only=True
                ) in html.letters:
                    line = stripped

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
            letters = canonical(line, letters_only=True)

            is_index_entry = bool(index_entry)

            def value_integrity_line() -> "CoverageLine | None":
                """Row value check, run only once the label+figures are
                confirmed present (a reliable anchor).  Catches sign flips,
                column-order swaps, currency and %—which the canonical text
                forms strip and would otherwise pass unseen."""
                if is_index_entry:
                    return None  # section numbers are not monetary figures
                row_figs = [
                    f
                    for f in _rich_figs(line, significant_only=True)
                    if f[0] not in section_ref_keys
                ]
                if not row_figs:
                    return None
                result.rows_with_figures += 1
                if line[:1].isdigit() or line[:2] in ("(1", "(2", "(3", "(4",
                                                       "(5", "(6", "(7", "(8",
                                                       "(9", "(0"):
                    # A line that starts with figures is a movement /
                    # reconciliation line, not a "label … figures" row; its
                    # values are covered by presence, count and sign checks.
                    result.rows_value_skipped["figures-first / movement line"] += 1
                    return None
                # Prose with figures embedded mid-sentence (e.g. "…net of
                # 9,098,409 (9,655,927) treasury shares as at June 30, 2025…")
                # is not a clean "label + value columns" row — the embedded
                # counts and dates defeat column matching.  If real words
                # continue after the first figure, treat it as prose.
                first_fig = next(
                    (
                        s
                        for s, _e, tok, key in iter_tokens(line)
                        if is_significant(key, tok)
                        and key not in section_ref_keys
                        and not re.match(r"^\(?0\d", tok.strip())
                    ),
                    None,
                )
                if first_fig is not None and len(
                    re.findall(r"[A-Za-z]", line[first_fig:])
                ) >= 8:
                    # ≥8 letters after the first figure = words continue after
                    # a value (pdfplumber often merges them: "treasuryshares
                    # asatjune"), so this is prose, not a value-column row.
                    result.rows_value_skipped[
                        "prose line with embedded figures"
                    ] += 1
                    return None
                # Distinctive, unambiguously-placed label only: too-short or
                # repeated-a-different-number-of-times labels risk mispairing,
                # and other checks already cover those lines.  (A lenient
                # any-occurrence fallback was trialled and rejected: generic
                # labels like "Total" are too weak an anchor and it produced
                # false positives.)
                if len(letters) < 12:
                    result.rows_value_skipped["short label (<12 chars)"] += 1
                    return None
                if prefix_ambiguous(letters):
                    result.rows_value_skipped[
                        "label is a prefix of a longer line"
                    ] += 1
                    return None
                if pdf_letters.count(letters) != html.letters.count(letters):
                    result.rows_value_skipped["label repeats unevenly"] += 1
                    return None
                result.rows_value_checked += 1
                next_letters = ""
                for nl in page_lines[line_no + 1 :]:
                    nl_letters = canonical(nl, letters_only=True)
                    if nl_letters:
                        next_letters = nl_letters[:30]
                        break
                abs_pos = page_letts_starts[page_idx] + line_letters_offset
                occ_index = pdf_letters.count(letters, 0, abs_pos)
                verdict, detail, html_read = _row_figures_near_label(
                    letters, row_figs, html, len(line), next_letters, occ_index
                )
                if verdict == "ok":
                    return None
                reads = f" (the HTML reads: “{html_read}”)" if html_read else ""
                headline = {
                    "row-value": "Row integrity — the HTML shows different "
                    "figures next to this label; values may have been swapped "
                    "between line items or mistyped.",
                    "column-order": "Column order — the same figures appear but "
                    "in a different left-to-right order; the comparative "
                    "columns (e.g. the two reporting periods) may be transposed.",
                    "sign": f"Sign — {detail}. A negative shown as positive (or "
                    "vice versa) changes the meaning.",
                    "currency": f"Currency — {detail}. The currency symbol "
                    "differs.",
                    "percent": f"Percentage — {detail}. A % was gained or lost.",
                }[verdict]
                status = "review" if verdict == "percent" else "missing"
                if status == "review":
                    result.review += 1
                return CoverageLine(
                    page_no,
                    line,
                    status,
                    f"{headline} In the PDF this row reads “{line}”{reads}. "
                    f"Compare against PDF {page_label}.",
                    label=page_label,
                    escalate=(status == "review"),
                    issue_kind=verdict,
                )

            # Tier 1: the whole line, figures included, appears verbatim.
            if c_alnum in html.alnum:
                # Text (incl. figures) matched — but sign/currency/% are
                # stripped by canonicalisation, so verify value integrity.
                vline = value_integrity_line()
                if vline is not None:
                    result.lines.append(vline)
                    continue
                result.ok += 1
                result.lines.append(CoverageLine(page_no, line, "ok", label=page_label))
                collect_anchor(line, page_label)
                continue

            text_ok = not letters or letters in html.letters
            missing_figs = [
                token
                for _s, _e, token, key in iter_tokens(line)
                if key not in html.number_keys and is_significant(key, token)
            ]

            if text_ok and not missing_figs:
                # Words match and every significant figure is present — verify
                # the figures sit beside THIS label with the right value,
                # order, sign and currency.
                vline = value_integrity_line()
                if vline is not None:
                    result.lines.append(vline)
                    continue
                result.ok += 1
                result.lines.append(CoverageLine(page_no, line, "ok", label=page_label))
                collect_anchor(line, page_label)
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
    _unit_scale_check(pages_raw, html, label_of, result)
    result.order_issues = _order_issues(anchors, label_of, result.lines)
    return result


#: unit-of-scale declarations that MUST be reproduced faithfully — a table
#: silently rescaled while the header is unchanged mis-states every figure
_UNIT_RE = re.compile(
    r"in\s+(?:₹|rs\.?|inr|us\$|\$|usd)\s*(crore|million|lakh|thousand|billion)",
    re.I,
)


def _unit_scale_check(pages_raw, html, label_of, result: CoverageResult) -> None:
    """Every unit-of-scale the PDF declares must appear in the HTML.

    "(In ₹ crore)" vs "(In ₹ million)" changes every figure by orders of
    magnitude, yet the digits still match — so the unit words themselves are
    checked explicitly.
    """
    from collections import Counter

    pdf_units: Counter = Counter()
    for raw in pages_raw:
        for m in _UNIT_RE.finditer(raw):
            pdf_units[m.group(1).lower()] += 1
    html_text = html.visible_text.lower()
    html_units = {
        u.lower(): len(re.findall(rf"\b{u}\b", html_text))
        for u in ("crore", "million", "lakh", "thousand", "billion")
    }
    for unit, pdf_n in pdf_units.items():
        if html_units.get(unit, 0) == 0:
            other = [u for u in html_units if html_units[u] and u != unit]
            result.review += 1
            result.total += 1
            result.lines.append(
                CoverageLine(
                    0,
                    f"(In ₹ {unit})",
                    "missing",
                    f"Unit of scale — the PDF reports figures “in ₹ {unit}” "
                    f"({pdf_n}× ) but the word “{unit}” never appears in the "
                    "HTML"
                    + (
                        f"; the HTML instead uses “{', '.join(other)}”. Every "
                        "figure would be mis-scaled"
                        if other
                        else ". Confirm the reporting unit is stated correctly"
                    )
                    + ".",
                    issue_kind="unit-scale",
                )
            )
