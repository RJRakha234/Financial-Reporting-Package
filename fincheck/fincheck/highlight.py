"""Produce an annotated copy of the PDF.

Every figure the checker summed is highlighted so coverage is visible at a
glance:

* **yellow** — a line item that was added into a total/subtotal (checked);
* **green** — a total/subtotal that foots correctly;
* **red**   — a total that does NOT foot (with an outline and a sticky note);
* **orange**— a total whose components could not be isolated (review manually).

PyMuPDF shares the PDF coordinate system used by pdfplumber, so cell bounding
boxes map straight onto page rectangles. A summary page with a legend and the
list of issues is prepended.
"""

import fitz  # PyMuPDF

from .checks import TotalCheck
from .numbers import format_number

_YELLOW = (0.99, 0.86, 0.30)
_GREEN = (0.30, 0.66, 0.36)
_RED = (0.86, 0.20, 0.18)
_ORANGE = (0.95, 0.60, 0.15)
_PAD = 1.5

# Higher priority wins when a cell would get more than one colour (a subtotal is
# both a total and a component of a grand total -> keep its total colour).
_PRIORITY = {"component": 0, "ok": 1, "unverified": 2, "error": 3}
_COLOR = {
    "component": _YELLOW,
    "ok": _GREEN,
    "unverified": _ORANGE,
    "error": _RED,
}


def _key(cell):
    return (round(cell.x0), round(cell.top))


def _annotate_page(
    page: "fitz.Page", checks: list[TotalCheck], show_components: bool
) -> None:
    # Resolve one colour per cell by priority, and collect notes for red totals.
    kind: dict[tuple, tuple] = {}  # key -> (priority, kind, cell)
    notes: dict[tuple, str] = {}

    def put(cell, k):
        key = _key(cell)
        prio = _PRIORITY[k]
        if key not in kind or prio > kind[key][0]:
            kind[key] = (prio, k, cell)

    for check in checks:
        if show_components:
            for c in check.component_cells:
                put(c, "component")
        put(check.total_cell, check.status)
        if check.status in ("error", "unverified"):
            notes[_key(check.total_cell)] = check.message()

    for prio, k, cell in kind.values():
        x0, top, x1, bottom = cell.bbox
        rect = fitz.Rect(x0 - _PAD, top - _PAD, x1 + _PAD, bottom + _PAD)
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=_COLOR[k])
        note = notes.get(_key(cell))
        if note:
            annot.set_info(content=note)
        annot.update()
        if k == "error":
            box = page.add_rect_annot(rect)
            box.set_colors(stroke=_RED)
            box.set_border(width=1.2)
            box.update()


def _swatch(page, x, y, color, label, font):
    page.draw_rect(fitz.Rect(x, y - 8, x + 16, y + 2), color=color, fill=color)
    page.insert_text((x + 24, y), label, fontsize=10, fontname=font)


def _add_summary_page(doc: "fitz.Document", checks: list[TotalCheck]) -> None:
    errors = [c for c in checks if c.status == "error"]
    unverified = [c for c in checks if c.status == "unverified"]
    ok = [c for c in checks if c.status == "ok"]
    figures = sum(len(c.component_cells) for c in checks)

    page = doc.new_page(0)
    insert_at = 1
    y = 54
    page.insert_text((54, y), "Financial Consistency Report", fontsize=20, fontname="hebo")
    y += 26
    page.insert_text(
        (54, y),
        f"Checked {len(ok) + len(errors)} totals/subtotals covering "
        f"{figures} figures.",
        fontsize=11,
        fontname="helv",
    )
    y += 16
    page.insert_text(
        (54, y),
        f"{len(errors)} do not foot · {len(ok)} foot · "
        f"{len(unverified)} could not be verified.",
        fontsize=11,
        fontname="helv",
    )
    y += 26

    # Legend.
    _swatch(page, 54, y, _YELLOW, "checked figure (summed into a total)", "helv")
    y += 16
    _swatch(page, 54, y, _GREEN, "total / subtotal that foots", "helv")
    y += 16
    _swatch(page, 54, y, _RED, "total that does NOT foot", "helv")
    y += 16
    _swatch(page, 54, y, _ORANGE, "total that could not be verified", "helv")
    y += 28

    for n, c in enumerate(errors, 1):
        if y > page.rect.height - 70:
            page = doc.new_page(insert_at)
            insert_at += 1
            y = 54
        page.insert_text(
            (54, y),
            f"{n}. Page {c.page_index + 1} — {c.label.strip()[:60]}",
            fontsize=11,
            fontname="hebo",
        )
        y += 15
        page.insert_text(
            (54, y),
            f"   stated {format_number(c.stated)}, sum of figures "
            f"{format_number(c.expected)} (off by {format_number(c.difference)})",
            fontsize=10,
            fontname="helv",
            color=_RED,
        )
        y += 22


def write_highlighted_pdf(
    source_pdf: str,
    output_pdf: str,
    checks: list[TotalCheck],
    show_components: bool = True,
) -> str:
    doc = fitz.open(source_pdf)
    by_page: dict[int, list[TotalCheck]] = {}
    for check in checks:
        by_page.setdefault(check.page_index, []).append(check)

    for page_index, page_checks in by_page.items():
        _annotate_page(doc[page_index], page_checks, show_components)

    _add_summary_page(doc, checks)
    doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    return output_pdf
