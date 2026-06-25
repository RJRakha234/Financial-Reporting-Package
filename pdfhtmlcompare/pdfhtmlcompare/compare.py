"""Compare the financial tables of a published PDF against the filed HTML.

Scope, by design: this checks whether the **numbers** and the **wordings** of the
financial statements render the same in the HTML, and whether the **lines inside
the financial tables** are all present. It deliberately ignores formatting —
layout, ordering, page furniture, the table of contents, signatures, narrative
prose — none of which is statement data.

To do that it compares only **financial lines**: a short labelled row that
carries at least one real figure (years, identifiers, clause/note references and
footnote markers are not figures — see :mod:`pdfhtmlcompare.numbers`). The
financial lines of the two documents are aligned by their label; within a matched
row the figures and the label wording are compared; a financial line with no
counterpart is a line missing from (or added to) a table.
"""

from dataclasses import dataclass, field
from difflib import SequenceMatcher

import os

from .htmldoc import read_html_lines
from .model import Figure, Line
from .numbers import format_number
from .pdfdoc import merge_pdfs, read_pdf_lines

# Finding kinds.
FIGURE_CHANGED = "figure_changed"
FIGURE_MISSING = "figure_missing"
FIGURE_EXTRA = "figure_extra"
WORD_CHANGED = "word_changed"
LINE_MISSING = "line_missing"
LINE_EXTRA = "line_extra"

# Row verdicts (used to colour the PDF / comment the HTML).
VALIDATED = "validated"
CHANGED = "changed"

_PAIR_THRESHOLD = 0.6
_RECONCILE_THRESHOLD = 0.85


@dataclass
class Finding:
    kind: str
    page: int | None
    label: str
    pdf_text: str
    html_text: str
    bbox: tuple[float, float, float, float] | None = None

    @property
    def page_label(self) -> str:
        return f"Page {self.page + 1}" if self.page is not None else "Page —"

    def message(self) -> str:
        ctx = f"  ·  {self.label.strip()[:70]}" if self.label.strip() else ""
        if self.kind == FIGURE_CHANGED:
            return f"Number differs: PDF shows {self.pdf_text}, HTML shows {self.html_text}.{ctx}"
        if self.kind == FIGURE_MISSING:
            return f"Number {self.pdf_text} is in the PDF row but missing from the HTML.{ctx}"
        if self.kind == FIGURE_EXTRA:
            return f"Number {self.html_text} is in the HTML row but not in the PDF.{ctx}"
        if self.kind == WORD_CHANGED:
            return f'Wording differs: PDF "{self.pdf_text}" vs HTML "{self.html_text}".'
        if self.kind == LINE_MISSING:
            return f"Table line missing from HTML (present in the PDF): {self.pdf_text.strip()[:90]}"
        if self.kind == LINE_EXTRA:
            return f"Table line only in HTML (not in the PDF): {self.html_text.strip()[:90]}"
        return f"{self.kind}: {self.pdf_text} / {self.html_text}"


@dataclass
class RowResult:
    status: str  # VALIDATED | CHANGED | LINE_MISSING | LINE_EXTRA
    pdf_line: Line | None
    html_line: Line | None
    findings: list[Finding] = field(default_factory=list)
    html_anchor: int | None = None  # for LINE_MISSING: html line to note it after

    def messages(self) -> list[str]:
        if self.status == VALIDATED:
            return ["matches the PDF"]
        if self.status == LINE_EXTRA:
            return ["this line is not in the published PDF"]
        return [f.message() for f in self.findings]


@dataclass
class ComparisonResult:
    source_pdf: str
    source_html: str
    rows: list[RowResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    pdf_figures: int = 0
    matched_figures: int = 0
    output_pdf: str | None = None
    output_html: str | None = None

    @property
    def consistent(self) -> bool:
        return not self.findings

    def by_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    @property
    def validated_rows(self) -> int:
        return sum(1 for r in self.rows if r.status == VALIDATED)


# --------------------------------------------------------------------------- #
# Cell-level: comparing the figures within one matched row.
# --------------------------------------------------------------------------- #


def _pair_by_value(pa: list[Figure], ha: list[Figure]):
    cands = sorted(
        (abs(p.value - h.value), ip, ih)
        for ip, p in enumerate(pa)
        for ih, h in enumerate(ha)
    )
    used_p, used_h, pairs = set(), set(), []
    for _, ip, ih in cands:
        if ip in used_p or ih in used_h:
            continue
        used_p.add(ip)
        used_h.add(ih)
        pairs.append((pa[ip], ha[ih]))
    lp = [pa[i] for i in range(len(pa)) if i not in used_p]
    lh = [ha[i] for i in range(len(ha)) if i not in used_h]
    return pairs, lp, lh


def _diff_figures(pf: list[Figure], hf: list[Figure], page: int | None) -> list[Finding]:
    a = [round(x.value, 2) for x in pf]
    b = [round(x.value, 2) for x in hf]
    findings: list[Finding] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        pa, ha = pf[i1:i2], hf[j1:j2]
        if tag == "delete":
            findings += [_fig(FIGURE_MISSING, p, None, page) for p in pa]
        elif tag == "insert":
            findings += [_fig(FIGURE_EXTRA, None, h, page) for h in ha]
        else:
            pairs, lp, lh = _pair_by_value(pa, ha)
            findings += [_fig(FIGURE_CHANGED, p, h, page) for p, h in pairs]
            findings += [_fig(FIGURE_MISSING, p, None, page) for p in lp]
            findings += [_fig(FIGURE_EXTRA, None, h, page) for h in lh]
    return findings


def _fig(kind, p: Figure | None, h: Figure | None, page) -> Finding:
    return Finding(
        kind=kind,
        page=(p.page if p else page),
        label="",
        pdf_text=(p.text if p else ""),
        html_text=(h.text if h else ""),
        bbox=(p.bbox if p else None),
    )


def _diff_row(pdf_line: Line, html_line: Line) -> list[Finding]:
    findings = _diff_figures(pdf_line.figures, html_line.figures, pdf_line.page)
    for f in findings:
        f.label = pdf_line.label_key
    if pdf_line.label_key != html_line.label_key:
        findings.append(
            Finding(
                WORD_CHANGED,
                pdf_line.page,
                pdf_line.label_key,
                " ".join(pdf_line.word_texts),
                " ".join(html_line.word_texts),
                pdf_line.bbox,
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# Line-level: aligning the financial rows of the two documents.
# --------------------------------------------------------------------------- #


def _pair_replace_block(pdf_block, html_block):
    scores = []
    for ip, p in enumerate(pdf_block):
        for ih, h in enumerate(html_block):
            r = SequenceMatcher(None, p.label_key, h.label_key).ratio()
            if r >= _PAIR_THRESHOLD:
                scores.append((r, ip, ih))
    scores.sort(reverse=True)
    used_p, used_h, pairs = set(), set(), []
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


def _align(pdf_fin: list[Line], html_fin: list[Line]) -> list[RowResult]:
    a = [l.label_key for l in pdf_fin]
    b = [l.label_key for l in html_fin]
    results: list[RowResult] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                results.append(_matched(pdf_fin[i1 + k], html_fin[j1 + k]))
        elif tag == "delete":
            results += [_missing(p) for p in pdf_fin[i1:i2]]
        elif tag == "insert":
            results += [_extra(h) for h in html_fin[j1:j2]]
        else:
            for p, h in _pair_replace_block(pdf_fin[i1:i2], html_fin[j1:j2]):
                if p is not None and h is not None:
                    results.append(_matched(p, h))
                elif p is not None:
                    results.append(_missing(p))
                else:
                    results.append(_extra(h))
    _reconcile_orphans(results)
    _anchor_missing(results)
    return results


def _matched(p: Line, h: Line) -> RowResult:
    findings = _diff_row(p, h)
    return RowResult(CHANGED if findings else VALIDATED, p, h, findings)


def _missing(p: Line) -> RowResult:
    return RowResult(
        LINE_MISSING, p, None,
        [Finding(LINE_MISSING, p.page, p.label_key, p.text, "", p.bbox)],
    )


def _extra(h: Line) -> RowResult:
    return RowResult(
        LINE_EXTRA, None, h,
        [Finding(LINE_EXTRA, None, h.label_key, "", h.text)],
    )


def _reconcile_orphans(results: list[RowResult]) -> None:
    """Fold a displaced-but-identical row that the aligner split into a separate
    missing and extra run back into a single matched row."""
    missing = [r for r in results if r.status == LINE_MISSING]
    extra = [r for r in results if r.status == LINE_EXTRA]
    if not missing or not extra:
        return
    used: set[int] = set()
    for rm in missing:
        best_i, best_r = None, 0.0
        for i, re_ in enumerate(extra):
            if i in used:
                continue
            r = SequenceMatcher(None, rm.pdf_line.label_key, re_.html_line.label_key).ratio()
            if r > best_r:
                best_r, best_i = r, i
        if best_i is not None and best_r >= _RECONCILE_THRESHOLD:
            used.add(best_i)
            re_ = extra[best_i]
            findings = _diff_row(rm.pdf_line, re_.html_line)
            rm.status = CHANGED if findings else VALIDATED
            rm.html_line = re_.html_line
            rm.findings = findings
            re_.status = "_consumed"
            re_.findings = []
    results[:] = [r for r in results if r.status != "_consumed"]


def _anchor_missing(results: list[RowResult]) -> None:
    last_html_idx: int | None = None
    for r in results:
        if r.html_line is not None:
            last_html_idx = r.html_line.index
        elif r.status == LINE_MISSING:
            r.html_anchor = last_html_idx


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #


def compare(
    pdf_path: "str | list[str]",
    html_path: str,
    output_pdf: str | None = None,
    output_html: str | None = None,
) -> ComparisonResult:
    """Compare the financial tables of the PDF(s) against ``html_path``.

    ``pdf_path`` may be a single path or a list of paths. Several PDFs (e.g. the
    auditor's report and the financial statements) are concatenated in order so
    they line up with one filed HTML, with page numbers running continuously.
    """
    pdf_paths = [pdf_path] if isinstance(pdf_path, str) else list(pdf_path)
    if not pdf_paths:
        raise ValueError("at least one PDF path is required")
    temp_pdf = merge_pdfs(pdf_paths) if len(pdf_paths) > 1 else None
    effective_pdf = temp_pdf or pdf_paths[0]

    try:
        return _compare(effective_pdf, html_path, pdf_paths, output_pdf, output_html)
    finally:
        if temp_pdf and os.path.exists(temp_pdf):
            os.remove(temp_pdf)


def _compare(effective_pdf, html_path, pdf_paths, output_pdf, output_html):
    pdf_lines = read_pdf_lines(effective_pdf)
    html_lines = read_html_lines(html_path)
    pdf_fin = [l for l in pdf_lines if l.is_financial]
    html_fin = [l for l in html_lines if l.is_financial]

    rows = _align(pdf_fin, html_fin)

    findings: list[Finding] = []
    flagged = 0
    for r in rows:
        if r.status == CHANGED:
            findings.extend(r.findings)
            flagged += sum(1 for f in r.findings if f.kind in (FIGURE_CHANGED, FIGURE_MISSING))
        elif r.status == LINE_MISSING:
            findings.append(r.findings[0])
            flagged += len(r.pdf_line.figures)
        elif r.status == LINE_EXTRA:
            findings.append(r.findings[0])

    findings.sort(
        key=lambda f: (f.page if f.page is not None else 1 << 30, f.kind)
    )

    pdf_figures = sum(len(l.figures) for l in pdf_fin)
    result = ComparisonResult(
        source_pdf=" + ".join(pdf_paths),
        source_html=html_path,
        rows=rows,
        findings=findings,
        pdf_figures=pdf_figures,
        matched_figures=max(pdf_figures - flagged, 0),
    )

    if output_pdf is not None:
        from .annotate_pdf import write_validated_pdf

        result.output_pdf = write_validated_pdf(effective_pdf, output_pdf, result)
    if output_html is not None:
        from .annotate_html import write_commented_html

        result.output_html = write_commented_html(html_path, output_html, result)
    return result
