"""Write a numbered copy of each source PDF alongside the comparison report.

The report tells you what differs; these tell you where it came from. Every
section in the report carries a serial number, and the same number is stamped
onto the region of each source PDF the section was drawn from. Read a point in
the report, note its number, open the marked-up file at the page it names, and
the passage is outlined and labelled.

Colour carries the verdict, so a page can be skimmed without the report at all:
green where the two documents agree, amber where they differ, red where the
section exists in one document only.
"""

import fitz  # PyMuPDF

_SAME = (0.18, 0.45, 0.32)
_CHANGED = (0.80, 0.45, 0.10)
_ONLY = (0.72, 0.20, 0.14)
_PAD = 2.0
# Serial badges sit in the margin; this is how wide a gutter they need.
_BADGE_W = 22.0
_BADGE_H = 13.0

_COLOUR = {
    "same": _SAME,
    "changed": _CHANGED,
    "added": _ONLY,
    "removed": _ONLY,
}


def _legend(page: "fitz.Page") -> None:
    x, y = 34, 14
    for colour, text in (
        (_SAME, "agrees"),
        (_CHANGED, "differs"),
        (_ONLY, "one side only"),
    ):
        page.draw_rect(
            fitz.Rect(x, y - 6, x + 8, y + 1), color=colour, fill=colour, width=0
        )
        page.insert_text((x + 12, y), text, fontsize=6.5, fontname="helv", color=colour)
        x += 26 + len(text) * 3.1


def _stamp(page: "fitz.Page", box, serial: int, colour, marked: str | None) -> None:
    """Outline one section's region and number it in the margin."""
    x0, y0, x1, y1 = box
    rect = fitz.Rect(x0 - _PAD, y0 - _PAD, x1 + _PAD, y1 + _PAD)
    page.draw_rect(rect, color=colour, width=0.7, dashes="[2 2] 0")

    # Put the badge in the left margin when there is room, and inside the
    # region when there is not, so it never lands off the page.
    left = rect.x0 - _BADGE_W - 3
    if left < 2:
        left = rect.x0 + 1
    badge = fitz.Rect(left, rect.y0, left + _BADGE_W, rect.y0 + _BADGE_H)
    page.draw_rect(badge, color=colour, fill=colour, width=0)
    page.insert_text(
        (badge.x0 + 3, badge.y0 + 9.5),
        str(serial),
        fontsize=8,
        fontname="hebo",
        color=(1, 1, 1),
    )
    if marked is not None:
        page.insert_text(
            (badge.x0 + 1, badge.y1 + 8),
            f"§{marked}",
            fontsize=6,
            fontname="helv",
            color=colour,
        )


def write_marked_copy(
    source_pdf: str, sections: list, side: str, output_pdf: str
) -> str:
    """Copy ``source_pdf`` with every compared section outlined and numbered."""
    doc = fitz.open(source_pdf)
    try:
        stamped = set()
        for section in sections:
            colour = _COLOUR.get(section.status, _CHANGED)
            for page_number, box in sorted(section.regions(side).items()):
                page = doc[page_number - 1]
                _stamp(page, box, section.serial, colour, section.marked)
                stamped.add(page_number)
        for number in sorted(stamped):
            _legend(doc[number - 1])
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
