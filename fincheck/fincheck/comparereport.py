"""Render a PDF↔HTML comparison as console text, JSON, HTML, or an annotated PDF.

The annotated PDF is the headline deliverable: every discrepancy is highlighted
on the exact spot of the *published* PDF and carries a sticky-note comment, so
the output is literally "the published document, with a page-referenced comment
on everything that does not match the HTML filed with the SEC". HTML-only
findings (a figure the conversion invented, with no place in the PDF) cannot be
pinned to a box, so they are collected on a prepended summary page that also
lists every finding with its page number.
"""

import json

import fitz  # PyMuPDF

from .compare import (
    CHANGED,
    EXTRA_IN_HTML,
    MISSING_IN_HTML,
    TEXT_CHANGED,
    ComparisonResult,
    Difference,
)

_RED = (0.86, 0.20, 0.18)
_ORANGE = (0.95, 0.60, 0.15)
_BLUE = (0.20, 0.45, 0.85)
_AMBER = (0.95, 0.80, 0.25)
_PAD = 1.5

_COLOR = {
    CHANGED: _RED,
    MISSING_IN_HTML: _ORANGE,
    EXTRA_IN_HTML: _BLUE,
    TEXT_CHANGED: _AMBER,
}
_LABEL = {
    CHANGED: "figure changed in HTML",
    MISSING_IN_HTML: "figure missing from HTML",
    EXTRA_IN_HTML: "figure only in HTML (not in PDF)",
    TEXT_CHANGED: "wording differs",
}


# --------------------------------------------------------------------------- #
# Text / JSON reports
# --------------------------------------------------------------------------- #


def to_dict(result: ComparisonResult) -> dict:
    return {
        "source_pdf": result.source_pdf,
        "source_html": result.source_html,
        "consistent": result.consistent,
        "difference_count": len(result.differences),
        "pdf_numbers": result.pdf_number_count,
        "html_numbers": result.html_number_count,
        "matched_numbers": result.matched_numbers,
        "differences": [
            {
                "kind": d.kind,
                "category": d.category,
                "page": (d.page + 1) if d.page is not None else None,
                "context": d.context.strip(),
                "pdf": d.pdf_text,
                "html": d.html_text,
                "message": d.message(),
            }
            for d in result.differences
        ],
    }


def to_json(result: ComparisonResult) -> str:
    return json.dumps(to_dict(result), indent=2)


def to_console(result: ComparisonResult) -> str:
    nums = result.number_differences
    texts = result.text_differences
    if not result.differences:
        return (
            "✓ PDF and HTML match: "
            f"{result.matched_numbers}/{result.pdf_number_count} figures "
            "aligned, no wording differences."
        )

    lines = [
        f"✗ Found {len(result.differences)} difference"
        f"{'' if len(result.differences) == 1 else 's'} "
        f"({len(nums)} figure, {len(texts)} wording):",
        "",
    ]
    for n, d in enumerate(result.differences, 1):
        lines.append(f"  {n}. {d.page_label}  ·  [{_LABEL[d.kind]}]")
        lines.append(f"     {d.message()}")
        lines.append("")
    lines.append(
        f"Figures: {result.matched_numbers} matched, "
        f"{len(nums)} flagged "
        f"(PDF {result.pdf_number_count} / HTML {result.html_number_count})."
    )
    return "\n".join(lines)


def to_html(result: ComparisonResult) -> str:
    rows = []
    for d in result.differences:
        color = "#%02x%02x%02x" % tuple(int(c * 255) for c in _COLOR[d.kind])
        rows.append(
            f"<tr>"
            f"<td>{d.page_label}</td>"
            f'<td><span style="color:{color};font-weight:600">{_LABEL[d.kind]}</span></td>'
            f"<td>{_esc(d.pdf_text)}</td>"
            f"<td>{_esc(d.html_text)}</td>"
            f"<td>{_esc(d.context.strip()[:80])}</td>"
            f"</tr>"
        )
    status = (
        "PDF and HTML match."
        if result.consistent
        else f"{len(result.differences)} difference(s) found."
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>PDF vs HTML comparison</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:2rem;color:#1a1a1a}}
 table{{border-collapse:collapse;width:100%;font-size:14px}}
 th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left;vertical-align:top}}
 th{{background:#f4f4f4}}
 caption{{text-align:left;font-size:13px;color:#555;margin-bottom:8px}}
</style></head><body>
<h1>Published PDF vs filed HTML</h1>
<p><b>{_esc(status)}</b> &nbsp; Figures: {result.matched_numbers} matched of
 {result.pdf_number_count} in the PDF ({result.html_number_count} in the HTML).</p>
<table>
 <caption>PDF: {_esc(result.source_pdf)} &nbsp;|&nbsp; HTML: {_esc(result.source_html)}</caption>
 <tr><th>PDF page</th><th>Issue</th><th>PDF</th><th>HTML</th><th>Context</th></tr>
 {''.join(rows) or '<tr><td colspan=5>No differences.</td></tr>'}
</table></body></html>"""


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------------- #
# Annotated PDF
# --------------------------------------------------------------------------- #


def _annotate_page(page: "fitz.Page", diffs: list[Difference]) -> None:
    for d in diffs:
        if d.bbox is None:
            continue
        x0, top, x1, bottom = d.bbox
        rect = fitz.Rect(x0 - _PAD, top - _PAD, x1 + _PAD, bottom + _PAD)
        color = _COLOR[d.kind]
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=color)
        annot.set_info(content=d.message())
        annot.update()
        # Figure changes are the costliest error; box them so they stand out.
        if d.kind == CHANGED:
            box = page.add_rect_annot(rect)
            box.set_colors(stroke=color)
            box.set_border(width=1.2)
            box.update()


def _swatch(page, x, y, color, label):
    page.draw_rect(fitz.Rect(x, y - 8, x + 16, y + 2), color=color, fill=color)
    page.insert_text((x + 24, y), label, fontsize=10, fontname="helv")


def _add_summary_pages(doc: "fitz.Document", result: ComparisonResult) -> None:
    page = doc.new_page(0)
    insert_at = 1
    y = 54
    page.insert_text((54, y), "PDF vs HTML Comparison", fontsize=20, fontname="hebo")
    y += 26
    page.insert_text(
        (54, y),
        f"{len(result.differences)} difference(s). "
        f"Figures: {result.matched_numbers} matched of {result.pdf_number_count} "
        f"in the PDF ({result.html_number_count} in the HTML).",
        fontsize=11,
        fontname="helv",
    )
    y += 26

    for kind, color in (
        (CHANGED, _RED),
        (MISSING_IN_HTML, _ORANGE),
        (EXTRA_IN_HTML, _BLUE),
        (TEXT_CHANGED, _AMBER),
    ):
        _swatch(page, 54, y, color, _LABEL[kind])
        y += 16
    y += 14

    for n, d in enumerate(result.differences, 1):
        if y > page.rect.height - 80:
            page = doc.new_page(insert_at)
            insert_at += 1
            y = 54
        page.insert_text(
            (54, y),
            f"{n}. {d.page_label} — {_LABEL[d.kind]}",
            fontsize=11,
            fontname="hebo",
        )
        y += 14
        for chunk in _wrap(d.message(), 96):
            page.insert_text((66, y), chunk, fontsize=9.5, fontname="helv")
            y += 13
        y += 8


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def write_compared_pdf(
    pdf_path: str, output_pdf: str, result: ComparisonResult
) -> str:
    doc = fitz.open(pdf_path)
    by_page: dict[int, list[Difference]] = {}
    for d in result.differences:
        if d.bbox is not None and d.page is not None:
            by_page.setdefault(d.page, []).append(d)
    for page_index, page_diffs in by_page.items():
        _annotate_page(doc[page_index], page_diffs)

    _add_summary_pages(doc, result)
    doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    return output_pdf
