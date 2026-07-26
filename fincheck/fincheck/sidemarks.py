"""Write a numbered, coverage-marked copy of each source PDF.

The report says what differs. These say two further things the report cannot:
**where** each point came from, and **what was covered**.

Every passage and every figure the report compared is tinted on the page it was
taken from, coloured by what the comparison found. So anything left untinted was
not compared — a paragraph the reconstruction missed, a table it did not read —
and that is visible at a glance rather than having to be taken on trust. Reading
these two files beside the report, a reviewer can confirm the report's coverage
without re-reading the documents.

Each section also carries its serial number from the report, in the margin and
in the PDF's bookmarks, so a point in the report opens directly at its source.
"""

import fitz  # PyMuPDF

_SAME = (0.17, 0.44, 0.31)
_CHANGED = (0.80, 0.45, 0.10)
_ONLY = (0.72, 0.20, 0.14)
# Tints are pale enough to read the text straight through.
_TINT = {
    "same": (0.87, 0.94, 0.90),
    "formatting": (0.87, 0.93, 0.95),
    "changed": (0.99, 0.92, 0.82),
    "added": (0.98, 0.88, 0.85),
    "removed": (0.98, 0.88, 0.85),
    "label-differs": (0.99, 0.92, 0.82),
    "figures-differ": (0.99, 0.86, 0.84),
    "columns-differ": (0.99, 0.86, 0.84),
}
_FORMATTING = (0.20, 0.42, 0.52)
_EDGE = {
    "same": _SAME,
    "formatting": _FORMATTING,
    "changed": _CHANGED,
    "added": _ONLY,
    "removed": _ONLY,
}
_PAD = 1.4
_BADGE_W = 21.0
_BADGE_H = 12.5
# A figure whose value differs gets an outline as well as a tint.
_FLAGGED = ("figures-differ", "columns-differ")


def _tint(page, box, colour, pad=_PAD) -> None:
    rect = fitz.Rect(box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
    page.draw_rect(rect, color=None, fill=colour, width=0, overlay=False)


def _outline(page, box, colour, width=0.6, dashes=None) -> None:
    rect = fitz.Rect(box[0] - _PAD, box[1] - _PAD, box[2] + _PAD, box[3] + _PAD)
    page.draw_rect(rect, color=colour, width=width, dashes=dashes)


def _cover_pair(page, unit, status: str, changed_columns: set) -> None:
    """Tint one compared passage, and every figure inside it, on its own page."""
    tint = _TINT.get(status, _TINT["changed"])
    for row in unit.rows:
        if row.page != page.number + 1:
            continue
        _tint(page, row.bbox, tint)
        for index, figure in enumerate(row.figures):
            box = (figure.x0, row.y0, figure.x1, row.y1)
            if index in changed_columns:
                _tint(page, box, _TINT["figures-differ"], pad=0.8)
                _outline(page, box, _ONLY, width=0.7)


def _stamp_section(page, box, serial: int, colour, marked: str | None) -> None:
    """Outline a section and number it in the margin."""
    _outline(page, box, colour, width=0.7, dashes="[2 2] 0")
    left = box[0] - _BADGE_W - 4
    if left < 2:
        left = box[0] + 1
    badge = fitz.Rect(left, box[1] - _PAD, left + _BADGE_W, box[1] - _PAD + _BADGE_H)
    page.draw_rect(badge, color=colour, fill=colour, width=0)
    page.insert_text(
        (badge.x0 + 3, badge.y0 + 9),
        str(serial),
        fontsize=7.5,
        fontname="hebo",
        color=(1, 1, 1),
    )
    if marked is not None:
        page.insert_text(
            (badge.x0 + 1, badge.y1 + 7.5),
            f"§{marked}",
            fontsize=5.5,
            fontname="helv",
            color=colour,
        )


def _legend(page) -> None:
    x, y = 34, 13
    page.insert_text(
        (x, y), "Compared:", fontsize=6.5, fontname="hebo", color=(0.3, 0.3, 0.3)
    )
    x += 42
    for status, text in (
        ("same", "agrees"),
        ("changed", "differs"),
        ("added", "one side only"),
    ):
        page.draw_rect(
            fitz.Rect(x, y - 6, x + 9, y + 1.5),
            color=_EDGE[status],
            fill=_TINT[status],
            width=0.4,
        )
        page.insert_text(
            (x + 12, y), text, fontsize=6.5, fontname="helv", color=_EDGE[status]
        )
        x += 30 + len(text) * 3.0
    page.insert_text(
        (x + 6, y),
        "untinted = not covered by the report",
        fontsize=6.5,
        fontname="helv",
        color=(0.45, 0.45, 0.45),
    )


def write_marked_copy(
    source_pdf: str, sections: list, side: str, output_pdf: str
) -> str:
    """Copy ``source_pdf`` with everything the report compared marked on it."""
    doc = fitz.open(source_pdf)
    try:
        touched: set = set()
        toc: list = []

        for section in sections:
            colour = _EDGE.get(section.status, _CHANGED)

            # Coverage first, so section outlines and badges sit on top of it.
            for pair in section.pairs:
                unit = pair.a if side == "a" else pair.b
                if unit is None:
                    continue
                changed = {i for i, _, _ in pair.changed_figures}
                for number in {r.page for r in unit.rows}:
                    if 1 <= number <= doc.page_count:
                        _cover_pair(doc[number - 1], unit, pair.status, changed)
                        touched.add(number)

            regions = sorted(section.regions(side).items())
            for number, box in regions:
                _stamp_section(doc[number - 1], box, section.serial, colour, section.marked)
                touched.add(number)
            if regions:
                title = f"{section.serial}. {section.title[:70]}"
                if section.marked is not None:
                    title = f"{section.serial}. §{section.marked} {section.title[:64]}"
                toc.append([1, title, regions[0][0]])

        for number in sorted(touched):
            _legend(doc[number - 1])
        if toc:
            # Bookmarks let a reader jump straight to a serial from the report.
            doc.set_toc(toc)
        doc.save(output_pdf, garbage=4, deflate=True)
    finally:
        doc.close()
    return output_pdf


def write_marked_copies(
    pdf_a: str, pdf_b: str, sections: list, out_a: str, out_b: str
) -> tuple[str, str]:
    return (
        write_marked_copy(pdf_a, sections, "a", out_a),
        write_marked_copy(pdf_b, sections, "b", out_b),
    )


def coverage(sections: list, side: str) -> tuple[int, int]:
    """How many passages and figures the report covered on one side."""
    passages = figures = 0
    for section in sections:
        for pair in section.pairs:
            unit = pair.a if side == "a" else pair.b
            if unit is None:
                continue
            passages += 1
            figures += sum(len(r.figures) for r in unit.rows)
    return passages, figures
