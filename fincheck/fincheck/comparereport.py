"""Render a PDF↔HTML comparison.

The headline output is the **validated PDF**: a copy of the published document in
which every figure that was checked against the HTML is highlighted **green**,
and every discrepancy is marked in colour with a sticky-note comment — so the
reviewer literally sees, on the published pages, what was validated and what was
not. Console, JSON, and a standalone HTML summary are also available.
"""

import json

import fitz  # PyMuPDF

from .compare import (
    CHANGED,
    EXTRA_IN_HTML,
    MISSING_IN_HTML,
    ROW_EXTRA,
    ROW_MISSING,
    TEXT_CHANGED,
    VALIDATED,
    ComparisonResult,
    Difference,
)

_GREEN = (0.30, 0.66, 0.36)
_RED = (0.86, 0.20, 0.18)
_ORANGE = (0.95, 0.60, 0.15)
_AMBER = (0.95, 0.80, 0.25)
_BLUE = (0.20, 0.45, 0.85)
_PAD = 1.5

# Higher priority wins when a box would get more than one colour.
_PRIORITY = {VALIDATED: 0, TEXT_CHANGED: 1, MISSING_IN_HTML: 2, ROW_MISSING: 3, CHANGED: 4}
_COLOR = {
    VALIDATED: _GREEN,
    CHANGED: _RED,
    MISSING_IN_HTML: _ORANGE,
    ROW_MISSING: _ORANGE,
    TEXT_CHANGED: _AMBER,
    EXTRA_IN_HTML: _BLUE,
    ROW_EXTRA: _BLUE,
}
_LABEL = {
    VALIDATED: "validated (matches HTML)",
    CHANGED: "figure changed in HTML",
    MISSING_IN_HTML: "figure missing from HTML row",
    ROW_MISSING: "row dropped in HTML",
    TEXT_CHANGED: "wording differs",
    EXTRA_IN_HTML: "figure only in HTML",
    ROW_EXTRA: "row only in HTML",
}

_SUMMARY_CAP = 300


# --------------------------------------------------------------------------- #
# Text / JSON / HTML-summary reports
# --------------------------------------------------------------------------- #


def to_dict(result: ComparisonResult) -> dict:
    return {
        "source_pdf": result.source_pdf,
        "source_html": result.source_html,
        "consistent": result.consistent,
        "difference_count": len(result.differences),
        "validated_rows": result.validated_rows,
        "rows_dropped_in_html": len(result.by_kind(ROW_MISSING)),
        "rows_only_in_html": len(result.by_kind(ROW_EXTRA)),
        "figures_changed": len(result.by_kind(CHANGED)),
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
    if not result.differences:
        return (
            "✓ PDF and HTML match: "
            f"{result.validated_rows} rows validated, "
            f"{result.matched_numbers}/{result.pdf_number_count} figures aligned, "
            "no dropped rows or wording differences."
        )

    rows_missing = result.by_kind(ROW_MISSING)
    rows_extra = result.by_kind(ROW_EXTRA)
    nums = result.number_differences
    texts = result.text_differences

    lines = [
        f"✗ Found {len(result.differences)} difference"
        f"{'' if len(result.differences) == 1 else 's'}:",
        f"    {len(rows_missing)} row(s) dropped in HTML, "
        f"{len(rows_extra)} row(s) only in HTML, "
        f"{len(nums)} figure, {len(texts)} wording.",
        "",
    ]
    shown = result.differences[:80]
    for n, d in enumerate(shown, 1):
        lines.append(f"  {n}. {d.page_label}  ·  [{_LABEL[d.kind]}]")
        lines.append(f"     {d.message()}")
        lines.append("")
    if len(result.differences) > len(shown):
        lines.append(f"  … and {len(result.differences) - len(shown)} more (see PDF / JSON).")
        lines.append("")
    lines.append(
        f"Rows validated: {result.validated_rows}. "
        f"Figures: {result.matched_numbers} matched of {result.pdf_number_count} "
        f"in the PDF ({result.html_number_count} in the HTML)."
    )
    return "\n".join(lines)


def to_html(result: ComparisonResult) -> str:
    rows = []
    for d in result.differences:
        color = "#%02x%02x%02x" % tuple(int(c * 255) for c in _COLOR[d.kind])
        rows.append(
            f"<tr><td>{d.page_label}</td>"
            f'<td><span style="color:{color};font-weight:600">{_LABEL[d.kind]}</span></td>'
            f"<td>{_esc(d.pdf_text[:90])}</td>"
            f"<td>{_esc(d.html_text[:90])}</td>"
            f"<td>{_esc(d.context.strip()[:80])}</td></tr>"
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
</style></head><body>
<h1>Published PDF vs filed HTML</h1>
<p><b>{_esc(status)}</b> &nbsp; {result.validated_rows} rows validated &nbsp;|&nbsp;
 Figures: {result.matched_numbers} matched of {result.pdf_number_count}.</p>
<table>
 <tr><th>PDF page</th><th>Issue</th><th>PDF</th><th>HTML</th><th>Context</th></tr>
 {''.join(rows) or '<tr><td colspan=5>No differences.</td></tr>'}
</table></body></html>"""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# Validated PDF
# --------------------------------------------------------------------------- #


def _key(bbox):
    return (round(bbox[0]), round(bbox[1]), round(bbox[2]))


def _collect_marks(result: ComparisonResult) -> dict[int, dict]:
    """page -> {key -> (priority, kind, bbox, note)}."""
    pages: dict[int, dict] = {}

    def put(page, bbox, kind, note):
        if bbox is None or page is None:
            return
        slot = pages.setdefault(page, {})
        k = _key(bbox)
        prio = _PRIORITY.get(kind, 0)
        if k not in slot or prio > slot[k][0]:
            slot[k] = (prio, kind, bbox, note)

    for r in result.line_results:
        pl = r.pdf_line
        if pl is None:
            continue
        if r.status == ROW_MISSING:
            put(pl.page, pl.bbox, ROW_MISSING, r.diffs[0].message())
            continue
        flagged: set = set()
        for d in r.diffs:
            if d.bbox is not None:
                put(pl.page, d.bbox, d.kind, d.message())
                flagged.add(_key(d.bbox))
        # Green everything that was actually validated against the HTML.
        for num in pl.numbers:
            if num.bbox is not None and _key(num.bbox) not in flagged:
                put(pl.page, num.bbox, VALIDATED, "Validated: matches the HTML filing.")
    return pages


def _annotate_page(page: "fitz.Page", slot: dict) -> None:
    for _prio, kind, bbox, note in slot.values():
        x0, top, x1, bottom = bbox
        rect = fitz.Rect(x0 - _PAD, top - _PAD, x1 + _PAD, bottom + _PAD)
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=_COLOR[kind])
        if note:
            annot.set_info(content=note)
        annot.update()
        if kind in (CHANGED, ROW_MISSING):
            box = page.add_rect_annot(rect)
            box.set_colors(stroke=_COLOR[kind])
            box.set_border(width=1.2)
            box.update()


def _swatch(page, x, y, color, label):
    page.draw_rect(fitz.Rect(x, y - 8, x + 16, y + 2), color=color, fill=color)
    page.insert_text((x + 24, y), label, fontsize=10, fontname="helv")


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    cur = ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out or [""]


def _add_summary_pages(doc: "fitz.Document", result: ComparisonResult) -> None:
    page = doc.new_page(0)
    insert_at = 1
    y = 54
    page.insert_text((54, y), "PDF vs HTML Comparison", fontsize=20, fontname="hebo")
    y += 24
    page.insert_text(
        (54, y),
        f"{result.validated_rows} rows validated · {len(result.differences)} difference(s).",
        fontsize=11,
        fontname="hebo",
    )
    y += 16
    page.insert_text(
        (54, y),
        f"{len(result.by_kind(ROW_MISSING))} rows dropped in HTML · "
        f"{len(result.by_kind(ROW_EXTRA))} rows only in HTML · "
        f"{len(result.by_kind(CHANGED))} figures changed · "
        f"{len(result.text_differences)} wording.",
        fontsize=11,
        fontname="helv",
    )
    y += 16
    page.insert_text(
        (54, y),
        f"Figures matched: {result.matched_numbers} of {result.pdf_number_count} "
        f"in the PDF ({result.html_number_count} in the HTML).",
        fontsize=11,
        fontname="helv",
    )
    y += 26

    for kind in (VALIDATED, CHANGED, MISSING_IN_HTML, ROW_MISSING, TEXT_CHANGED, ROW_EXTRA):
        _swatch(page, 54, y, _COLOR[kind], _LABEL[kind])
        y += 16
    y += 12

    shown = result.differences[:_SUMMARY_CAP]
    for n, d in enumerate(shown, 1):
        if y > page.rect.height - 80:
            page = doc.new_page(insert_at)
            insert_at += 1
            y = 54
        page.insert_text(
            (54, y), f"{n}. {d.page_label} — {_LABEL[d.kind]}", fontsize=10.5, fontname="hebo"
        )
        y += 13
        for chunk in _wrap(d.message(), 100):
            page.insert_text((66, y), chunk, fontsize=9, fontname="helv")
            y += 12
        y += 6
    if len(result.differences) > len(shown):
        page.insert_text(
            (54, y),
            f"… and {len(result.differences) - len(shown)} more (see the JSON report).",
            fontsize=10,
            fontname="helv",
        )


def write_validated_pdf(pdf_path: str, output_pdf: str, result: ComparisonResult) -> str:
    doc = fitz.open(pdf_path)
    marks = _collect_marks(result)
    for page_index, slot in marks.items():
        if 0 <= page_index < doc.page_count:
            _annotate_page(doc[page_index], slot)
    _add_summary_pages(doc, result)
    doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    return output_pdf
