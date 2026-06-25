"""Compare the **whole content** of a published PDF against the filed HTML.

Every word and every number of the PDF is checked against the HTML, not just the
financial figures. The two documents are reduced to ordered streams of tokens
(words + numbers) and aligned; whatever is changed, missing, or added is reported
and anchored to the PDF page it came from. Comparing token *streams* (rather than
lines) makes the result immune to the fact that the PDF wraps text to the page
while the HTML keeps a paragraph per block.

The only things excluded are pagination furniture that is not document content:
running page headers/footers (the same line repeated across many pages) and lone
page numbers. Everything else — statements, notes, the auditor's report, headings
— is compared.
"""

import os
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .htmldoc import read_html_lines
from .model import NUMBER, WORD, Line, Token
from .pdfdoc import merge_pdfs, read_pdf_lines_multi

# Finding kinds.
CHANGED = "changed"            # content differs between PDF and HTML
MISSING = "missing_from_html"  # in the PDF, absent from the HTML
ADDED = "added_in_html"        # in the HTML, absent from the PDF

# A line repeated on at least this many pages is a running header/footer.
_HEADER_REPEAT_PAGES = 5
# Cap how much text a single finding prints.
_RUN_CAP = 140


@dataclass
class Finding:
    kind: str
    page: int | None
    pdf_text: str
    html_text: str
    pdf_tokens: list[Token] = field(default_factory=list)
    html_line_ids: list[int] = field(default_factory=list)  # CHANGED/ADDED: lines touched
    html_anchor: int | None = None  # for MISSING: html line id to note it after

    @property
    def page_label(self) -> str:
        return f"Page {self.page + 1}" if self.page is not None else "Page —"

    def _is_one(self, kind):
        toks = self.pdf_tokens
        return len(toks) == 1 and toks[0].kind == kind

    def message(self) -> str:
        if self.kind == CHANGED:
            if self._is_one(NUMBER):
                return f"Number differs: PDF shows {self.pdf_text}, HTML shows {self.html_text}."
            kind_word = "Word" if self._is_one(WORD) else "Content"
            return f'{kind_word} differs: PDF "{self.pdf_text}" vs HTML "{self.html_text}".'
        if self.kind == MISSING:
            return f'Missing from HTML (present in the PDF): "{self.pdf_text}".'
        return f'Added in HTML (not in the PDF): "{self.html_text}".'


@dataclass
class ComparisonResult:
    source_pdf: str
    source_html: str
    findings: list[Finding] = field(default_factory=list)
    pdf_lines: list[Line] = field(default_factory=list)
    html_lines: list[Line] = field(default_factory=list)
    pdf_tokens: int = 0
    matched_tokens: int = 0
    output_pdf: str | None = None
    output_html: str | None = None

    @property
    def consistent(self) -> bool:
        return not self.findings

    @property
    def coverage(self) -> float:
        return (self.matched_tokens / self.pdf_tokens) if self.pdf_tokens else 1.0

    def by_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


# --------------------------------------------------------------------------- #
# Filtering pagination furniture (not document content).
# --------------------------------------------------------------------------- #


def _running_headers(pdf_lines: list[Line]) -> set[str]:
    pages_of: dict[str, set] = defaultdict(set)
    for l in pdf_lines:
        key = " ".join(l.text.lower().split())
        if key:
            pages_of[key].add(l.page)
    return {k for k, ps in pages_of.items() if len(ps) >= _HEADER_REPEAT_PAGES}


def _is_furniture(line: Line, headers: set[str]) -> bool:
    if " ".join(line.text.lower().split()) in headers:
        return True
    nums = [t for t in line.tokens if t.kind == NUMBER]
    words = [t for t in line.tokens if t.kind == WORD]
    # A lone number on its own line is a page number, not content.
    if not words and len(nums) <= 1:
        return True
    # Rotated column headers (e.g. the statement of changes in equity) extract as
    # a row of 1–2 character fragments — unreadable garbage, not content.
    if len(words) >= 4 and sum(len(t.text) <= 2 for t in words) / len(words) > 0.7:
        return True
    return False


def _content_pdf_lines(pdf_lines: list[Line], headers: set[str]) -> list[Line]:
    return [l for l in pdf_lines if not _is_furniture(l, headers)]


def _content_html_lines(html_lines: list[Line], headers: set[str]) -> list[Line]:
    """Drop the same running headers/footers from the HTML, so a PDF header that
    is filtered out is not then reported as 'added in HTML'."""
    return [
        l for l in html_lines
        if " ".join(l.text.lower().split()) not in headers
    ]


# --------------------------------------------------------------------------- #
# Aligning the two token streams.
# --------------------------------------------------------------------------- #


def _nearest_page(tokens: list[Token], i: int) -> int | None:
    if not tokens:
        return None
    return tokens[min(max(i, 0), len(tokens) - 1)].page


def _runs_text(tokens: list[Token]) -> str:
    s = " ".join(t.text for t in tokens)
    return (s[:_RUN_CAP] + "…") if len(s) > _RUN_CAP else s


def _align(pdf_tokens: list[Token], html_tokens: list[Token]) -> list[Finding]:
    a = [(t.kind, t.key) for t in pdf_tokens]
    b = [(t.kind, t.key) for t in html_tokens]
    sm = SequenceMatcher(a=a, b=b, autojunk=False)
    findings: list[Finding] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        pruns, hruns = pdf_tokens[i1:i2], html_tokens[j1:j2]
        if tag == "equal":
            for t in pruns:
                t.status = "matched"
            continue
        if tag == "replace":
            for t in pruns:
                t.status = "changed"
            for t in hruns:
                t.status = "changed"
            findings.append(
                Finding(
                    CHANGED, pruns[0].page, _runs_text(pruns), _runs_text(hruns),
                    list(pruns), html_line_ids=_line_ids(hruns),
                )
            )
        elif tag == "delete":
            for t in pruns:
                t.status = "missing"
            findings.append(
                Finding(
                    MISSING, pruns[0].page, _runs_text(pruns), "",
                    list(pruns), html_anchor=_nearest_line(html_tokens, j1 - 1),
                )
            )
        else:  # insert
            for t in hruns:
                t.status = "added"
            findings.append(
                Finding(
                    ADDED, _nearest_page(pdf_tokens, i1), "", _runs_text(hruns),
                    [], html_line_ids=_line_ids(hruns),
                )
            )
    return findings


def _line_ids(tokens: list[Token]) -> list[int]:
    seen = []
    for t in tokens:
        if t.line_id not in seen:
            seen.append(t.line_id)
    return seen


def _nearest_line(tokens: list[Token], i: int) -> int | None:
    if not tokens or i < 0:
        return None
    return tokens[min(i, len(tokens) - 1)].line_id


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #


def compare(pdf_path, html_path, output_pdf=None, output_html=None) -> ComparisonResult:
    """Compare the whole content of the PDF(s) against ``html_path``.

    ``pdf_path`` may be a single path or a list of paths (concatenated in order).
    """
    pdf_paths = [pdf_path] if isinstance(pdf_path, str) else list(pdf_path)
    if not pdf_paths:
        raise ValueError("at least one PDF path is required")

    # Read each PDF on its own (intact text extraction); merge a copy only to
    # annotate, where geometry — not text — is what matters.
    pdf_all = read_pdf_lines_multi(pdf_paths)
    html_all = read_html_lines(html_path)
    headers = _running_headers(pdf_all)
    pdf_lines = _content_pdf_lines(pdf_all, headers)
    html_lines = _content_html_lines(html_all, headers)

    pdf_tokens = [t for l in pdf_lines for t in l.tokens]
    html_tokens = [t for l in html_lines for t in l.tokens]

    findings = _align(pdf_tokens, html_tokens)
    findings.sort(key=lambda f: (f.page if f.page is not None else 1 << 30, f.kind))

    matched = sum(1 for t in pdf_tokens if t.status == "matched")
    result = ComparisonResult(
        source_pdf=" + ".join(pdf_paths),
        source_html=html_path,
        findings=findings,
        pdf_lines=pdf_lines,
        html_lines=html_lines,
        pdf_tokens=len(pdf_tokens),
        matched_tokens=matched,
    )

    if output_pdf is not None:
        from .annotate_pdf import write_validated_pdf

        temp_pdf = merge_pdfs(pdf_paths) if len(pdf_paths) > 1 else None
        try:
            result.output_pdf = write_validated_pdf(
                temp_pdf or pdf_paths[0], output_pdf, result
            )
        finally:
            if temp_pdf and os.path.exists(temp_pdf):
                os.remove(temp_pdf)
    if output_html is not None:
        from .annotate_html import write_commented_html

        result.output_html = write_commented_html(html_path, output_html, result)
    return result
