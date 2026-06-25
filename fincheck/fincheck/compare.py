"""Compare a published financial PDF against the HTML filed with the SEC.

At quarter end the same statements that are published (PDF) are converted to
HTML for the SEC filing. The conversion can silently drop a whole table row,
transpose a digit, lose a minus sign, or change a word. This module reads both
documents, aligns them **row by row**, and reports every discrepancy tied back
to the **page of the PDF** it came from.

Two levels of alignment run:

1. **Line level** — the logical lines (table rows, headings, paragraphs) of the
   two documents are aligned. A PDF line with no match in the HTML is a *missing
   row* (a dropped table line); an HTML line with no match in the PDF is an
   *extra row*. This is what catches table-formatting misses.
2. **Cell level** — within a matched row, the figures and words are aligned, so a
   changed figure, a dropped cell, or a changed word inside an otherwise-matching
   row is pinpointed.

Matched content is recorded too (not just differences), so the PDF can be
green-highlighted everywhere it was validated and the HTML can be annotated with
a ``✓ matches`` comment on every row that checks out.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .extract import extract_lines
from .htmlextract import read_html_lines
from .numbers import is_numberish, parse_number

# Cell-level difference kinds (within a matched row).
CHANGED = "changed"
MISSING_IN_HTML = "missing_in_html"
EXTRA_IN_HTML = "extra_in_html"
TEXT_CHANGED = "text_changed"
# Row-level kinds (a whole logical line).
ROW_MISSING = "row_missing"
ROW_EXTRA = "row_extra"

# Per-row validation status, used to colour the PDF and comment the HTML.
VALIDATED = "validated"
LINE_CHANGED = "line_changed"
# A non-data line (prose/heading) that did not line up. Real filings break prose
# differently in PDF (wrapped to page width) and HTML (one paragraph per block),
# so an unmatched line with no figures is *not* claimed as a dropped row — it is
# left uncompared rather than reported as a false discrepancy.
SKIPPED = "skipped"

# A PDF line repeated on at least this many pages is treated as a running
# header/footer and excluded from the row comparison.
_HEADER_REPEAT_PAGES = 5

# A logical line whose label runs longer than this (in words) is prose, not a
# table row — it must not be reported as a dropped/added row, since PDF and HTML
# wrap prose differently. Real statement labels are short ("Total equity ...").
_MAX_ROW_LABEL_WORDS = 10

# Parenthesised single digits are footnote/superscript markers, not figures.
_FOOTNOTE_RE = re.compile(r"^\(\d\)$")

# Minimum text similarity to treat two non-identical rows as "the same row that
# changed" rather than one dropped and one added.
_PAIR_THRESHOLD = 0.6


@dataclass
class Num:
    """A figure read from one of the documents, with where it came from."""

    value: float
    text: str
    context: str
    order: int
    page: int | None = None  # 0-based PDF page; None for the HTML side
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class Word:
    norm: str
    text: str
    order: int
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class DocLine:
    """One logical line (table row / heading / paragraph) of a document."""

    index: int
    text: str
    label: str
    numbers: list[Num] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class Difference:
    """A single mismatch between the PDF and the HTML, anchored to a PDF page."""

    kind: str
    category: str  # "number", "text", or "row"
    page: int | None
    context: str
    pdf_text: str
    html_text: str
    bbox: tuple[float, float, float, float] | None = None

    @property
    def page_label(self) -> str:
        return f"Page {self.page + 1}" if self.page is not None else "Page —"

    def message(self) -> str:
        ctx = f"  ·  {self.context.strip()[:70]}" if self.context.strip() else ""
        if self.kind == CHANGED:
            return f"Figure mismatch: PDF shows {self.pdf_text}, HTML shows {self.html_text}.{ctx}"
        if self.kind == MISSING_IN_HTML:
            return f"Figure {self.pdf_text} is in the PDF but missing from this row in the HTML.{ctx}"
        if self.kind == EXTRA_IN_HTML:
            return f"Figure {self.html_text} is in the HTML row but not in the PDF.{ctx}"
        if self.kind == TEXT_CHANGED:
            pdf = f'"{self.pdf_text}"' if self.pdf_text else "(nothing)"
            html = f'"{self.html_text}"' if self.html_text else "(nothing)"
            return f"Wording differs: PDF {pdf} vs HTML {html}.{ctx}"
        if self.kind == ROW_MISSING:
            return f"Row dropped in HTML (present in the PDF): {self.pdf_text.strip()[:90]}"
        if self.kind == ROW_EXTRA:
            return f"Row only in HTML (not in the published PDF): {self.html_text.strip()[:90]}"
        return f"{self.kind}: {self.pdf_text} / {self.html_text}.{ctx}"


@dataclass
class LineResult:
    """The verdict on one row, used to render both outputs."""

    status: str  # VALIDATED | LINE_CHANGED | ROW_MISSING | ROW_EXTRA
    pdf_line: DocLine | None
    html_line: DocLine | None
    diffs: list[Difference] = field(default_factory=list)
    html_anchor: int | None = None  # for ROW_MISSING: html line to note it after

    def messages(self) -> list[str]:
        if self.status == VALIDATED:
            return ["matches the PDF"]
        if self.status == ROW_EXTRA:
            return ["this row is not in the published PDF"]
        return [d.message() for d in self.diffs]


@dataclass
class ComparisonResult:
    source_pdf: str
    source_html: str
    line_results: list[LineResult] = field(default_factory=list)
    differences: list[Difference] = field(default_factory=list)
    pdf_number_count: int = 0
    html_number_count: int = 0
    matched_numbers: int = 0
    output_pdf: str | None = None
    output_html: str | None = None

    @property
    def consistent(self) -> bool:
        return not self.differences

    def by_kind(self, kind: str) -> list[Difference]:
        return [d for d in self.differences if d.kind == kind]

    @property
    def number_differences(self) -> list[Difference]:
        return [d for d in self.differences if d.category == "number"]

    @property
    def text_differences(self) -> list[Difference]:
        return [d for d in self.differences if d.category == "text"]

    @property
    def row_differences(self) -> list[Difference]:
        return [d for d in self.differences if d.category == "row"]

    @property
    def validated_rows(self) -> int:
        return sum(1 for r in self.line_results if r.status == VALIDATED)


# --------------------------------------------------------------------------- #
# Tokenising a line into a label and its figures.
# --------------------------------------------------------------------------- #


def _split_line(tokens: list[tuple[str, object]]) -> tuple[str, list[tuple[str, float, object]]]:
    """Split a line's tokens into (label text, [(text, value, payload), ...])."""
    labels: list[str] = []
    numbers: list[tuple[str, float, object]] = []
    for text, payload in tokens:
        if _FOOTNOTE_RE.match(text.strip()):
            continue  # footnote marker, neither a figure nor a meaningful label
        value = parse_number(text)
        if value is not None and is_numberish(text):
            numbers.append((text, value, payload))
        else:
            labels.append(text)
    return " ".join(labels).strip(), numbers


_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def _word_norms(token: str) -> list[str]:
    """Normalised sub-words of a token (may be several).

    Curly quotes are folded to straight and the token is split on any
    non-alphanumeric, so "Non-current" and "Non current" both yield
    ``["non", "current"]`` — hyphen-vs-space conversion artefacts do not
    masquerade as wording changes. Dotted index leaders and figures yield
    nothing (figures are compared separately, with full precision).
    """
    if is_numberish(token):
        return []
    t = token.translate(_QUOTES)
    if "…" in t or "..." in t:
        return []
    out = []
    for part in re.split(r"[^a-z0-9]+", t.lower()):
        if len(part) >= 2 and any(ch.isalpha() for ch in part):
            out.append(part)
    return out


def _has_leader(text: str) -> bool:
    return "…" in text or "..." in text


def _norm_line(text: str) -> str:
    return " ".join(text.translate(_QUOTES).lower().split())


def _label_word_count(label: str) -> int:
    return len(label.split())


def _is_data_row(line: "DocLine | None") -> bool:
    """A short, figure-bearing line — a real table row, not a prose sentence."""
    return bool(line and line.numbers) and _label_word_count(line.label) <= _MAX_ROW_LABEL_WORDS


def _pdf_doclines(pdf_path: str) -> list[DocLine]:
    lines: list[DocLine] = []
    idx = 0
    n_order = 0
    w_order = 0
    for page in extract_lines(pdf_path):
        last_label = ""
        for line in page.lines:
            tokens = [(t.text, t.bbox) for t in line]
            label, nums = _split_line(tokens)
            context = label or last_label
            if label:
                last_label = label
            numbers = []
            for text, value, bbox in nums:
                numbers.append(Num(value, text, context, n_order, page.index, bbox))
                n_order += 1
            words = []
            for t in line:
                for norm in _word_norms(t.text):
                    words.append(Word(norm, t.text, w_order, page.index, t.bbox))
                    w_order += 1
            xs0 = [t.x0 for t in line]
            ys0 = [t.top for t in line]
            xs1 = [t.x1 for t in line]
            ys1 = [t.bottom for t in line]
            bbox = (min(xs0), min(ys0), max(xs1), max(ys1)) if line else None
            text = " ".join(t.text for t in line)
            lines.append(
                DocLine(idx, text, context, numbers, words, page.index, bbox)
            )
            idx += 1
    return lines


def _html_doclines(html_path: str) -> list[DocLine]:
    lines: list[DocLine] = []
    last_label = ""
    n_order = 0
    w_order = 0
    for idx, raw in enumerate(read_html_lines(html_path)):
        toks = raw.split()
        label, nums = _split_line([(t, None) for t in toks])
        context = label or last_label
        if label:
            last_label = label
        numbers = []
        for text, value, _ in nums:
            numbers.append(Num(value, text, context, n_order))
            n_order += 1
        words = []
        for t in toks:
            for norm in _word_norms(t):
                words.append(Word(norm, t, w_order))
                w_order += 1
        lines.append(DocLine(idx, raw, context, numbers, words, None, None))
    return lines


# --------------------------------------------------------------------------- #
# Cell-level alignment (within a matched row), reused across the codebase.
# --------------------------------------------------------------------------- #


def _nearest_page(pdf_items, i1: int) -> int | None:
    if not pdf_items:
        return None
    idx = min(max(i1 - 1, 0), len(pdf_items) - 1)
    return pdf_items[idx].page


def _diff_numbers(pdf_nums: list[Num], html_nums: list[Num]) -> tuple[list[Difference], int]:
    pdf_keys = [round(n.value, 2) for n in pdf_nums]
    html_keys = [round(n.value, 2) for n in html_nums]
    sm = SequenceMatcher(a=pdf_keys, b=html_keys, autojunk=False)

    diffs: list[Difference] = []
    matched = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            matched += i2 - i1
            continue
        pa = pdf_nums[i1:i2]
        ha = html_nums[j1:j2]
        if tag == "delete":
            for p in pa:
                diffs.append(_num_diff(MISSING_IN_HTML, p, None))
        elif tag == "insert":
            page = _nearest_page(pdf_nums, i1)
            for h in ha:
                diffs.append(_num_diff(EXTRA_IN_HTML, None, h, page=page))
        else:
            pairs, leftover_p, leftover_h = _match_block(pa, ha)
            for p, h in pairs:
                diffs.append(_num_diff(CHANGED, p, h))
            for p in leftover_p:
                diffs.append(_num_diff(MISSING_IN_HTML, p, None))
            for h in leftover_h:
                page = _nearest_page(pdf_nums, i1)
                diffs.append(_num_diff(EXTRA_IN_HTML, None, h, page=page))
    return diffs, matched


def _num_diff(kind: str, p: Num | None, h: Num | None, page: int | None = None) -> Difference:
    context = (p.context if p else "") or (h.context if h else "")
    return Difference(
        kind=kind,
        category="number",
        page=p.page if p else page,
        context=context,
        pdf_text=p.text if p else "",
        html_text=h.text if h else "",
        bbox=p.bbox if p else None,
    )


def _ctx_key(n: Num) -> str:
    return " ".join(n.context.lower().split())


def _match_block(
    pa: list[Num], ha: list[Num]
) -> tuple[list[tuple[Num, Num]], list[Num], list[Num]]:
    """Pair PDF figures with HTML figures inside a mismatched block.

    Pairs greedily by *context first* (a figure on the same line label is almost
    certainly the same item) and then by closest value, so a transposed digit
    lines up with its own line and a genuinely added/removed figure is left over.
    """
    candidates: list[tuple[int, float, int, int]] = []
    for ip, p in enumerate(pa):
        for ih, h in enumerate(ha):
            same_ctx = bool(_ctx_key(p)) and _ctx_key(p) == _ctx_key(h)
            candidates.append((0 if same_ctx else 1, abs(p.value - h.value), ip, ih))
    candidates.sort()

    used_p: set[int] = set()
    used_h: set[int] = set()
    pairs: list[tuple[Num, Num]] = []
    for _, _, ip, ih in candidates:
        if ip in used_p or ih in used_h:
            continue
        used_p.add(ip)
        used_h.add(ih)
        pairs.append((pa[ip], ha[ih]))

    leftover_p = [pa[i] for i in range(len(pa)) if i not in used_p]
    leftover_h = [ha[i] for i in range(len(ha)) if i not in used_h]
    return pairs, leftover_p, leftover_h


def _diff_words(pdf_words: list[Word], html_words: list[Word]) -> list[Difference]:
    a = [w.norm for w in pdf_words]
    b = [w.norm for w in html_words]
    sm = SequenceMatcher(a=a, b=b, autojunk=False)

    diffs: list[Difference] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        pa = pdf_words[i1:i2]
        ha = html_words[j1:j2]
        pdf_run = " ".join(w.text for w in pa).strip()
        html_run = " ".join(w.text for w in ha).strip()
        if not pdf_run and not html_run:
            continue
        page = pa[0].page if pa else _nearest_page(pdf_words, i1)
        bbox = pa[0].bbox if pa else None
        diffs.append(
            Difference(
                kind=TEXT_CHANGED,
                category="text",
                page=page,
                context=pdf_run or html_run,
                pdf_text=pdf_run,
                html_text=html_run,
                bbox=bbox,
            )
        )
    return diffs


def _diff_line(pdf_line: DocLine, html_line: DocLine, compare_text: bool) -> list[Difference]:
    diffs, _ = _diff_numbers(pdf_line.numbers, html_line.numbers)
    # Word-level comparison only on short (non-prose) lines: across a wrapped
    # auditor's-report paragraph the word runs are reflow noise, not edits.
    if (
        compare_text
        and _label_word_count(pdf_line.label) <= _MAX_ROW_LABEL_WORDS
        and _label_word_count(html_line.label) <= _MAX_ROW_LABEL_WORDS
    ):
        diffs += _diff_words(pdf_line.words, html_line.words)
    return diffs


# --------------------------------------------------------------------------- #
# Line-level alignment.
# --------------------------------------------------------------------------- #


def _row_missing(pdf_line: DocLine) -> Difference:
    return Difference(
        kind=ROW_MISSING,
        category="row",
        page=pdf_line.page,
        context=pdf_line.label,
        pdf_text=pdf_line.text,
        html_text="",
        bbox=pdf_line.bbox,
    )


def _row_extra(html_line: DocLine, page: int | None) -> Difference:
    return Difference(
        kind=ROW_EXTRA,
        category="row",
        page=page,
        context=html_line.label,
        pdf_text="",
        html_text=html_line.text,
    )


def _pair_replace_block(pdf_block, html_block):
    """Greedily pair similar rows in a replace block; yield (pdf|None, html|None)."""
    scores = []
    for ip, p in enumerate(pdf_block):
        pk = _norm_line(p.text)
        for ih, h in enumerate(html_block):
            ratio = SequenceMatcher(None, pk, _norm_line(h.text)).ratio()
            if ratio >= _PAIR_THRESHOLD:
                scores.append((ratio, ip, ih))
    scores.sort(reverse=True)
    used_p: set[int] = set()
    used_h: set[int] = set()
    pairs: list[tuple] = []
    for _, ip, ih in scores:
        if ip in used_p or ih in used_h:
            continue
        used_p.add(ip)
        used_h.add(ih)
        pairs.append((pdf_block[ip], html_block[ih]))
    for ip, p in enumerate(pdf_block):
        if ip not in used_p:
            pairs.append((p, None))
    for ih, h in enumerate(html_block):
        if ih not in used_h:
            pairs.append((None, h))
    return pairs


def _running_headers(pdf_lines: list[DocLine]) -> set[str]:
    """Normalised texts that repeat across many pages — page headers/footers."""
    pages_of: dict[str, set] = defaultdict(set)
    for l in pdf_lines:
        key = _norm_line(l.text)
        if key:
            pages_of[key].add(l.page)
    return {k for k, ps in pages_of.items() if len(ps) >= _HEADER_REPEAT_PAGES}


def _comparable(pdf_lines: list[DocLine]) -> list[DocLine]:
    """Lines worth comparing: drop running headers/footers and index (dot-leader)
    entries, which are page furniture rather than statement content."""
    headers = _running_headers(pdf_lines)
    return [
        l
        for l in pdf_lines
        if _norm_line(l.text) not in headers and not _has_leader(l.text)
    ]


_RECONCILE_THRESHOLD = 0.85


def _reconcile_orphans(results: list[LineResult], compare_text: bool) -> None:
    """Pair up a dropped row with an added row of near-identical text.

    The global line aligner can split two copies of the same wide table row (e.g.
    a "Balance as at …" line in the statement of changes in equity) into a
    separate delete run and insert run that it never pairs. Re-match those
    leftovers by text similarity and fold them into one row result, so an
    unchanged-but-displaced row is not reported as both missing and extra.
    """
    missing = [r for r in results if r.status == ROW_MISSING]
    extra = [r for r in results if r.status == ROW_EXTRA]
    if not missing or not extra:
        return
    extra_keys = [_norm_line(r.html_line.text) for r in extra]
    used: set[int] = set()
    for rm in missing:
        pk = _norm_line(rm.pdf_line.text)
        best_i, best_r = None, 0.0
        for i, ek in enumerate(extra_keys):
            if i in used:
                continue
            ratio = SequenceMatcher(None, pk, ek).ratio()
            if ratio > best_r:
                best_r, best_i = ratio, i
        if best_i is not None and best_r >= _RECONCILE_THRESHOLD:
            used.add(best_i)
            re_ = extra[best_i]
            diffs = _diff_line(rm.pdf_line, re_.html_line, compare_text)
            rm.status = LINE_CHANGED if diffs else VALIDATED
            rm.html_line = re_.html_line
            rm.diffs = diffs
            re_.status = SKIPPED
            re_.diffs = []


def _demote_prose_gaps(results: list[LineResult]) -> None:
    """A dropped/extra line that is not a clear data row cannot be reliably
    row-matched across the PDF/HTML reflow, so leave it uncompared rather than
    cry false drop."""
    for r in results:
        if r.status == ROW_MISSING and not _is_data_row(r.pdf_line):
            r.status = SKIPPED
            r.diffs = []
        elif r.status == ROW_EXTRA and not _is_data_row(r.html_line):
            r.status = SKIPPED
            r.diffs = []


def _align_lines(pdf_lines, html_lines, compare_text) -> list[LineResult]:
    a = [_norm_line(l.text) for l in pdf_lines]
    b = [_norm_line(l.text) for l in html_lines]
    sm = SequenceMatcher(a=a, b=b, autojunk=False)

    results: list[LineResult] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                results.append(
                    LineResult(VALIDATED, pdf_lines[i1 + k], html_lines[j1 + k])
                )
        elif tag == "delete":
            for p in pdf_lines[i1:i2]:
                results.append(LineResult(ROW_MISSING, p, None, [_row_missing(p)]))
        elif tag == "insert":
            page = _nearest_page(pdf_lines, i1)
            for h in html_lines[j1:j2]:
                results.append(LineResult(ROW_EXTRA, None, h, [_row_extra(h, page)]))
        else:  # replace
            page = _nearest_page(pdf_lines, i1)
            for p, h in _pair_replace_block(pdf_lines[i1:i2], html_lines[j1:j2]):
                if p is not None and h is not None:
                    diffs = _diff_line(p, h, compare_text)
                    status = LINE_CHANGED if diffs else VALIDATED
                    results.append(LineResult(status, p, h, diffs))
                elif p is not None:
                    results.append(LineResult(ROW_MISSING, p, None, [_row_missing(p)]))
                else:
                    results.append(LineResult(ROW_EXTRA, None, h, [_row_extra(h, page)]))
    _reconcile_orphans(results, compare_text)
    _demote_prose_gaps(results)
    _anchor_missing_rows(results)
    return results


def _anchor_missing_rows(results: list[LineResult]) -> None:
    """Note each dropped row against the HTML line that precedes it, so the HTML
    annotation can flag "a row is missing here" at the right place."""
    last_html_idx: int | None = None
    for r in results:
        if r.html_line is not None:
            last_html_idx = r.html_line.index
        elif r.status == ROW_MISSING:
            r.html_anchor = last_html_idx


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #


def compare(
    pdf_path: str,
    html_path: str,
    output_pdf: str | None = None,
    output_html: str | None = None,
    compare_text: bool = True,
) -> ComparisonResult:
    """Compare a published PDF against the filed HTML.

    Args:
        pdf_path: the published financial-statement PDF.
        html_path: the HTML filed with the SEC (a conversion of the same doc).
        output_pdf: if given, write a copy of the PDF with **green** highlights
            on everything validated and coloured marks + comments on every
            discrepancy.
        output_html: if given, write a copy of the HTML with an inline ``✓/✗``
            comment on every row.
        compare_text: also compare wording, not just figures.
    """
    pdf_lines = _comparable(_pdf_doclines(pdf_path))
    html_lines = _html_doclines(html_path)
    line_results = _align_lines(pdf_lines, html_lines, compare_text)

    differences: list[Difference] = []
    flagged_pdf_numbers = 0
    for r in line_results:
        if r.status == ROW_MISSING and r.pdf_line is not None:
            differences.append(r.diffs[0])
            flagged_pdf_numbers += len(r.pdf_line.numbers)
        elif r.status == ROW_EXTRA:
            differences.append(r.diffs[0])
        elif r.status == LINE_CHANGED:
            differences.extend(r.diffs)
            flagged_pdf_numbers += sum(
                1 for d in r.diffs if d.kind in (CHANGED, MISSING_IN_HTML)
            )

    differences.sort(
        key=lambda d: (
            d.page if d.page is not None else 1 << 30,
            0 if d.category == "row" else (1 if d.category == "number" else 2),
        )
    )

    pdf_number_count = sum(len(l.numbers) for l in pdf_lines)
    result = ComparisonResult(
        source_pdf=pdf_path,
        source_html=html_path,
        line_results=line_results,
        differences=differences,
        pdf_number_count=pdf_number_count,
        html_number_count=sum(len(l.numbers) for l in html_lines),
        matched_numbers=max(pdf_number_count - flagged_pdf_numbers, 0),
    )

    if output_pdf is not None:
        from .comparereport import write_validated_pdf

        result.output_pdf = write_validated_pdf(pdf_path, output_pdf, result)
    if output_html is not None:
        from .htmlannotate import write_commented_html

        result.output_html = write_commented_html(html_path, output_html, result)
    return result
