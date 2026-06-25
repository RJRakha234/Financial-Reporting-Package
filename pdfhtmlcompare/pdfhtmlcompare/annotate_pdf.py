"""Write a copy of the published PDF marked up with the comparison result.

Every figure in a financial row that was validated against the HTML is
highlighted **green**; a changed number is boxed red, a number missing from the
HTML row is orange, a changed label is amber, and a whole table line dropped from
the HTML is boxed orange. Each mark carries a sticky-note comment, and a summary
page listing every finding (with its PDF page) is prepended. Non-financial
content is left untouched — only the financial tables are in scope.
"""

import fitz  # PyMuPDF

from .compare import (
    CHANGED,
    FIGURE_CHANGED,
    FIGURE_MISSING,
    LINE_MISSING,
    VALIDATED,
    WORD_CHANGED,
    ComparisonResult,
)

_GREEN = (0.30, 0.66, 0.36)
_RED = (0.86, 0.20, 0.18)
_ORANGE = (0.95, 0.60, 0.15)
_AMBER = (0.95, 0.80, 0.25)
_PAD = 1.5

_COLOR = {
    VALIDATED: _GREEN,
    FIGURE_CHANGED: _RED,
    FIGURE_MISSING: _ORANGE,
    WORD_CHANGED: _AMBER,
    LINE_MISSING: _ORANGE,
}
_LABEL = {
    VALIDATED: "validated (matches HTML)",
    FIGURE_CHANGED: "number changed in HTML",
    FIGURE_MISSING: "number missing from HTML row",
    WORD_CHANGED: "wording differs",
    LINE_MISSING: "table line missing from HTML",
}
_SUMMARY_CAP = 400


def _label_region(line):
    """The label part of a line's bbox (left of the first figure)."""
    x0, top, x1, bottom = line.bbox
    fig_x0 = [f.bbox[0] for f in line.figures if f.bbox]
    right = min(fig_x0) - 2 if fig_x0 else x1
    return (x0, top, max(right, x0 + 6), bottom)


def _collect(result: ComparisonResult):
    """page -> list of (bbox, kind, note, boxed)."""
    pages: dict[int, list] = {}

    def add(page, bbox, kind, note, boxed=False):
        if bbox is None or page is None:
            return
        pages.setdefault(page, []).append((bbox, kind, note, boxed))

    for r in result.rows:
        pl = r.pdf_line
        if pl is None:
            continue
        if r.status == LINE_MISSING:
            add(pl.page, pl.bbox, LINE_MISSING, r.findings[0].message(), boxed=True)
            continue
        if r.status not in (VALIDATED, CHANGED):
            continue
        flagged = set()
        for f in r.findings:
            if f.kind == WORD_CHANGED:
                add(pl.page, _label_region(pl), WORD_CHANGED, f.message())
            elif f.bbox is not None:
                add(pl.page, f.bbox, f.kind, f.message(), boxed=(f.kind == FIGURE_CHANGED))
                flagged.add(_key(f.bbox))
        for fig in pl.figures:
            if fig.bbox is not None and _key(fig.bbox) not in flagged:
                add(pl.page, fig.bbox, VALIDATED, "Validated: matches the HTML filing.")
    return pages


def _key(bbox):
    return (round(bbox[0]), round(bbox[1]), round(bbox[2]))


def _annotate_page(page, marks):
    for bbox, kind, note, boxed in marks:
        x0, top, x1, bottom = bbox
        rect = fitz.Rect(x0 - _PAD, top - _PAD, x1 + _PAD, bottom + _PAD)
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=_COLOR[kind])
        if note:
            annot.set_info(content=note)
        annot.update()
        if boxed:
            box = page.add_rect_annot(rect)
            box.set_colors(stroke=_COLOR[kind])
            box.set_border(width=1.2)
            box.update()


def _swatch(page, x, y, color, label):
    page.draw_rect(fitz.Rect(x, y - 8, x + 16, y + 2), color=color, fill=color)
    page.insert_text((x + 24, y), label, fontsize=10, fontname="helv")


def _wrap(text, width):
    out, cur = [], ""
    for w in text.split():
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out or [""]


def _summary(doc, result: ComparisonResult):
    page = doc.new_page(0)
    insert_at = 1
    y = 54
    page.insert_text((54, y), "PDF vs HTML — financial-table check", fontsize=18, fontname="hebo")
    y += 24
    page.insert_text(
        (54, y),
        f"{result.validated_rows} financial rows validated · {len(result.findings)} finding(s).",
        fontsize=11, fontname="hebo",
    )
    y += 16
    from .compare import FIGURE_CHANGED as FC, FIGURE_MISSING as FM, FIGURE_EXTRA as FE
    from .compare import LINE_EXTRA as LE, WORD_CHANGED as WC

    page.insert_text(
        (54, y),
        f"{len(result.by_kind(FC))} numbers changed · {len(result.by_kind(FM))} numbers missing · "
        f"{len(result.by_kind(FE))} numbers only in HTML · {len(result.by_kind(WC))} wording · "
        f"{len(result.by_kind(LINE_MISSING))} lines missing · {len(result.by_kind(LE))} lines only in HTML.",
        fontsize=10, fontname="helv",
    )
    y += 16
    page.insert_text(
        (54, y),
        f"Figures matched: {result.matched_figures} of {result.pdf_figures} in the PDF tables.",
        fontsize=11, fontname="helv",
    )
    y += 24
    for kind in (VALIDATED, FIGURE_CHANGED, FIGURE_MISSING, WORD_CHANGED, LINE_MISSING):
        _swatch(page, 54, y, _COLOR[kind], _LABEL[kind])
        y += 16
    y += 12

    shown = result.findings[:_SUMMARY_CAP]
    for n, f in enumerate(shown, 1):
        if y > page.rect.height - 80:
            page = doc.new_page(insert_at)
            insert_at += 1
            y = 54
        page.insert_text((54, y), f"{n}. {f.page_label}", fontsize=10, fontname="hebo")
        y += 13
        for chunk in _wrap(f.message(), 100):
            page.insert_text((66, y), chunk, fontsize=9, fontname="helv")
            y += 12
        y += 6
    if len(result.findings) > len(shown):
        page.insert_text(
            (54, y), f"… and {len(result.findings) - len(shown)} more (see the JSON report).",
            fontsize=10, fontname="helv",
        )


def write_validated_pdf(pdf_path: str, output_pdf: str, result: ComparisonResult) -> str:
    doc = fitz.open(pdf_path)
    for page_index, marks in _collect(result).items():
        if 0 <= page_index < doc.page_count:
            _annotate_page(doc[page_index], marks)
    _summary(doc, result)
    doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    return output_pdf
