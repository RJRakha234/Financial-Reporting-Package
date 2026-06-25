"""Compare a published financial PDF against the HTML filed with the SEC.

At quarter end the same statements that are published (PDF) are converted to
HTML for the SEC filing. The conversion can silently drop a line, transpose a
digit, lose a minus sign, or change a word. This module reads both documents,
aligns them, and reports every figure or wording that does not match — each
tied back to the **page of the PDF** it came from, so a reviewer can open the
published document at that page and check.

The strategy is alignment, not naive set-difference: because the HTML is a
conversion of the same content, the *order* of figures and words is largely
preserved. We therefore run :class:`difflib.SequenceMatcher` over the sequence
of numeric values (and, separately, the sequence of words) and report only the
stretches that fail to line up — which is exactly where a conversion error
shows. Matched figures are not reported, keeping the output focused on genuine
discrepancies.
"""

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .extract import extract_lines
from .htmlextract import read_html_lines
from .numbers import format_number, is_numberish, parse_number

# Number difference kinds.
CHANGED = "changed"
MISSING_IN_HTML = "missing_in_html"
EXTRA_IN_HTML = "extra_in_html"
TEXT_CHANGED = "text_changed"


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
class Difference:
    """A single mismatch between the PDF and the HTML.

    ``page`` is the 0-based PDF page the discrepancy is anchored to (the page a
    reviewer should open). ``bbox`` is the figure/word's box on that page when
    the PDF side is present, so it can be highlighted and commented in place.
    """

    kind: str
    category: str  # "number" or "text"
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
        if self.category == "number":
            if self.kind == CHANGED:
                return (
                    f"Figure mismatch: PDF shows {self.pdf_text}, "
                    f"HTML shows {self.html_text}.{ctx}"
                )
            if self.kind == MISSING_IN_HTML:
                return (
                    f"Figure {self.pdf_text} is in the published PDF but missing "
                    f"from the HTML here.{ctx}"
                )
            if self.kind == EXTRA_IN_HTML:
                return (
                    f"Figure {self.html_text} appears in the HTML but not in the "
                    f"published PDF here.{ctx}"
                )
        else:
            if self.kind == TEXT_CHANGED:
                pdf = f'"{self.pdf_text}"' if self.pdf_text else "(nothing)"
                html = f'"{self.html_text}"' if self.html_text else "(nothing)"
                return f"Wording differs: PDF {pdf} vs HTML {html}.{ctx}"
        return f"{self.kind}: {self.pdf_text} / {self.html_text}.{ctx}"


@dataclass
class ComparisonResult:
    source_pdf: str
    source_html: str
    differences: list[Difference] = field(default_factory=list)
    pdf_number_count: int = 0
    html_number_count: int = 0
    matched_numbers: int = 0
    output_pdf: str | None = None

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


# --------------------------------------------------------------------------- #
# Tokenising a line into a label and its figures.
# --------------------------------------------------------------------------- #


def _split_line(tokens: list[tuple[str, object]]) -> tuple[str, list[tuple[str, float, object]]]:
    """Split a line's tokens into (label text, [(text, value, payload), ...]).

    ``tokens`` is a list of ``(text, payload)``; payload is a bbox for PDF words
    and ``None`` for HTML. A token counts as a figure only when it both *looks*
    like a number and parses to one, so labels such as "Total" or a bare year
    inside a sentence are kept as text.
    """
    labels: list[str] = []
    numbers: list[tuple[str, float, object]] = []
    for text, payload in tokens:
        value = parse_number(text)
        if value is not None and is_numberish(text):
            numbers.append((text, value, payload))
        else:
            labels.append(text)
    return " ".join(labels).strip(), numbers


def _pdf_streams(pdf_path: str) -> tuple[list[Num], list[Word]]:
    numbers: list[Num] = []
    words: list[Word] = []
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
            for text, value, bbox in nums:
                numbers.append(
                    Num(value, text, context, n_order, page.index, bbox)
                )
                n_order += 1
            for tok in line:
                norm = _norm_word(tok.text)
                if norm:
                    words.append(Word(norm, tok.text, w_order, page.index, tok.bbox))
                    w_order += 1
    return numbers, words


def _html_streams(html_path: str) -> tuple[list[Num], list[Word]]:
    numbers: list[Num] = []
    words: list[Word] = []
    n_order = 0
    w_order = 0
    last_label = ""
    for line in read_html_lines(html_path):
        raw = line.split()
        tokens = [(t, None) for t in raw]
        label, nums = _split_line(tokens)
        context = label or last_label
        if label:
            last_label = label
        for text, value, _ in nums:
            numbers.append(Num(value, text, context, n_order))
            n_order += 1
        for tok in raw:
            norm = _norm_word(tok)
            if norm:
                words.append(Word(norm, tok, w_order))
                w_order += 1
    return numbers, words


_PUNCT = ".,;:()[]{}'\"`%–—-…*/\\|"


def _norm_word(token: str) -> str:
    """Normalise a token for word comparison; '' if it is not a real word."""
    if is_numberish(token):
        return ""  # figures are compared separately, with full precision
    s = token.strip().strip(_PUNCT).lower()
    if len(s) < 2 or not any(ch.isalpha() for ch in s):
        return ""
    return s


# --------------------------------------------------------------------------- #
# Aligning the two streams.
# --------------------------------------------------------------------------- #


def _nearest_page(pdf_items, i1: int) -> int | None:
    """PDF page to anchor an HTML-only difference to: the item just before it."""
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
        else:  # replace — pair figures within the block, leftovers are add/drop
            pairs, leftover_p, leftover_h = _match_block(pa, ha)
            for p, h in pairs:
                diffs.append(_num_diff(CHANGED, p, h))
            for p in leftover_p:
                diffs.append(_num_diff(MISSING_IN_HTML, p, None))
            for h in leftover_h:
                page = _nearest_page(pdf_nums, i1)
                diffs.append(_num_diff(EXTRA_IN_HTML, None, h, page=page))
    return diffs, matched


def _ctx_key(n: Num) -> str:
    return " ".join(n.context.lower().split())


def _match_block(
    pa: list[Num], ha: list[Num]
) -> tuple[list[tuple[Num, Num]], list[Num], list[Num]]:
    """Pair PDF figures with HTML figures inside a mismatched block.

    When a block mixes a changed figure with a dropped or invented one, naive
    position-by-position pairing mislabels them. Instead, pair greedily by
    *context first* (a figure on the same line label is almost certainly the
    same item) and then by closest value, so a transposed digit lines up with
    its own line and a genuinely added/removed figure is left over.
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


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #


def compare(
    pdf_path: str,
    html_path: str,
    output_pdf: str | None = None,
    compare_text: bool = True,
) -> ComparisonResult:
    """Compare a published PDF against the filed HTML.

    Args:
        pdf_path: the published financial-statement PDF.
        html_path: the HTML filed with the SEC (a conversion of the same doc).
        output_pdf: if given, write an annotated copy of the PDF with a
            page-referenced comment on every discrepancy.
        compare_text: also compare wording, not just figures.

    Returns:
        A :class:`ComparisonResult` listing every difference, each anchored to a
        PDF page.
    """
    pdf_nums, pdf_words = _pdf_streams(pdf_path)
    html_nums, html_words = _html_streams(html_path)

    number_diffs, matched = _diff_numbers(pdf_nums, html_nums)
    text_diffs = _diff_words(pdf_words, html_words) if compare_text else []

    differences = number_diffs + text_diffs
    # Order the report by PDF page so it reads top-to-bottom like the document;
    # figures before wording on the same page.
    differences.sort(
        key=lambda d: (
            d.page if d.page is not None else 1 << 30,
            0 if d.category == "number" else 1,
        )
    )

    result = ComparisonResult(
        source_pdf=pdf_path,
        source_html=html_path,
        differences=differences,
        pdf_number_count=len(pdf_nums),
        html_number_count=len(html_nums),
        matched_numbers=matched,
    )

    if output_pdf is not None:
        from .comparereport import write_compared_pdf

        result.output_pdf = write_compared_pdf(pdf_path, output_pdf, result)
    return result
