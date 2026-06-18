"""Write a copy of each PDF with every line colour-highlighted by status:

* **green**  — the line's language matched the other document;
* **red**    — the line's language did not match (changed wording, or a line
  present in one document but not the other).

A summary page with a legend and the match statistics is prepended.
"""

import fitz  # PyMuPDF

from .compare import Result
from .extract import Line

GREEN = (0.30, 0.78, 0.30)
RED = (0.95, 0.27, 0.27)
_STATUS_COLOR = {"match": GREEN, "mismatch": RED}


def _highlight_lines(doc: fitz.Document, lines: list[Line]) -> None:
    by_page: dict[int, list[Line]] = {}
    for ln in lines:
        if ln.status in _STATUS_COLOR:
            by_page.setdefault(ln.page, []).append(ln)
    for pno, page_lines in by_page.items():
        page = doc[pno]
        for ln in page_lines:
            rect = fitz.Rect(*ln.rect)
            if rect.is_empty or rect.is_infinite:
                continue
            annot = page.add_highlight_annot(rect)
            annot.set_colors(stroke=_STATUS_COLOR[ln.status])
            annot.set_opacity(0.45)
            annot.update()


def _add_summary_page(
    doc: fitz.Document, title: str, matched: int, mismatched: int, other: int
) -> None:
    page = doc.new_page(0, width=595, height=842)  # A4
    y = 60
    page.insert_text((50, y), "finmatch — language comparison", fontsize=18)
    y += 26
    page.insert_text((50, y), title, fontsize=11, color=(0.3, 0.3, 0.3))
    y += 40

    total = matched + mismatched
    pct = (matched / total * 100) if total else 100.0
    lines = [
        f"Lines matched (same language):   {matched}",
        f"Lines NOT matched:               {mismatched}",
        f"Language match rate:             {pct:.1f}%",
    ]
    if other:
        lines.append(f"Lines only in the other file:    {other}")
    for text in lines:
        page.insert_text((50, y), text, fontsize=12)
        y += 22

    y += 24
    page.insert_text((50, y), "Legend", fontsize=13)
    y += 10
    for color, label in (
        (GREEN, "matched — wording is identical (numbers ignored)"),
        (RED, "not matched — wording differs or line is missing"),
    ):
        y += 22
        page.draw_rect(fitz.Rect(50, y - 10, 80, y + 4), color=color, fill=color)
        page.insert_text((90, y), label, fontsize=11)

    y += 50
    note = (
        "Numbers (and currency symbols / unit words) are masked before "
        "comparison, so two currency versions of the same statement match on "
        "language. Review every red line."
    )
    page.insert_textbox(fitz.Rect(50, y, 545, y + 80), note, fontsize=10,
                        color=(0.3, 0.3, 0.3))


def highlight(result: Result, path_a: str, path_b: str, out_a: str, out_b: str) -> None:
    title_a = f"{path_a}   (compared against {path_b})"
    title_b = f"{path_b}   (compared against {path_a})"

    doc_a = fitz.open(path_a)
    try:
        _highlight_lines(doc_a, result.lines_a)
        _add_summary_page(
            doc_a, title_a, result.matched, result.mismatched_a, result.mismatched_b
        )
        doc_a.save(out_a, garbage=3, deflate=True)
    finally:
        doc_a.close()

    doc_b = fitz.open(path_b)
    try:
        _highlight_lines(doc_b, result.lines_b)
        _add_summary_page(
            doc_b, title_b, result.matched, result.mismatched_b, result.mismatched_a
        )
        doc_b.save(out_b, garbage=3, deflate=True)
    finally:
        doc_b.close()
