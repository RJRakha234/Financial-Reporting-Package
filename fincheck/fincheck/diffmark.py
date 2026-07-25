"""Write a marked-up copy of the second PDF showing exactly what changed.

Marks land on the B document, at the coordinates the comparison actually
observed, so nothing here re-derives position:

* **green**  — text present only in B (added);
* **red**    — text present only in A (removed), boxed where it used to sit;
* **amber**  — text edited in place, with the old string in a sticky note;
* **blue**   — text that only moved or changed style;
* **violet** — an outline around each region whose rendered pixels differ.

A summary page carrying the layered verdicts is prepended.
"""

import fitz  # PyMuPDF

from .compare import ComparisonResult
from .numbers import format_number

_GREEN = (0.30, 0.66, 0.36)
_RED = (0.86, 0.20, 0.18)
_AMBER = (0.95, 0.60, 0.15)
_BLUE = (0.24, 0.47, 0.85)
_VIOLET = (0.55, 0.36, 0.79)
_PAD = 1.5

_COLOR = {
    "added": _GREEN,
    "removed": _RED,
    "edited": _AMBER,
    "moved": _BLUE,
    "restyled": _BLUE,
    "moved+restyled": _BLUE,
}


def _note(change) -> str:
    if change.kind == "edited":
        text = f"edited: {change.before.text.strip()!r} → {change.after.text.strip()!r}"
        if change.is_numeric:
            text += (
                f"\nvalue {format_number(change.before.value)} → "
                f"{format_number(change.after.value)} "
                f"(change of {format_number(change.delta)})"
            )
        return text
    if change.kind == "removed":
        return f"removed: {change.before.text.strip()!r}"
    if change.kind == "added":
        return f"added: {change.after.text.strip()!r}"
    return (
        f"{change.kind}: {change.before.text.strip()!r}\n"
        f"{change.before.where()} → {change.after.where()}"
    )


def _mark_page(page: "fitz.Page", comparison) -> None:
    marked: list[fitz.Rect] = []

    for change in comparison.span_changes:
        span = change.after or change.before
        x0, y0, x1, y1 = span.bbox
        rect = fitz.Rect(x0 - _PAD, y0 - _PAD, x1 + _PAD, y1 + _PAD)
        color = _COLOR.get(change.kind, _AMBER)

        # A removed span has nothing under it on this page, so box it rather
        # than highlighting empty space.
        if change.kind == "removed":
            annot = page.add_rect_annot(rect)
            annot.set_border(width=1.2)
        else:
            annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=color)
        annot.set_info(content=_note(change))
        annot.update()
        marked.append(rect)

    if comparison.pixels is None:
        return

    # Only outline visual differences no text mark already explains — a moved
    # ruling line, a changed image. Boxing regions that merely restate a
    # highlighted edit would bury the ones that need a human eye.
    for region in comparison.pixels.regions:
        rect = fitz.Rect(*region.bbox)
        if any(rect.intersects(other) for other in marked):
            continue
        box = page.add_rect_annot(rect)
        box.set_colors(stroke=_VIOLET)
        box.set_border(width=0.8)
        box.set_info(
            content="rendered pixels differ here, with no text change to explain it"
        )
        box.update()


def _swatch(page, x, y, color, label):
    page.draw_rect(fitz.Rect(x, y - 8, x + 16, y + 2), color=color, fill=color)
    page.insert_text((x + 24, y), label, fontsize=10, fontname="helv")


def _verdict_text(result: ComparisonResult) -> list[tuple[str, tuple]]:
    def mark(ok):
        if ok is None:
            return "not checked", (0.4, 0.4, 0.4)
        return ("same", _GREEN) if ok else ("DIFFERS", _RED)

    rows = [
        ("byte-for-byte identical", result.bytes_identical),
        ("page count and geometry", result.geometry_identical),
        ("text, every character", result.text_identical),
        ("text spans, incl. font and position", result.spans_identical),
        ("vector graphics and images", result.graphics_identical),
    ]
    if result.pixels_compared:
        rows.append(
            (f"rendered pixels at {result.dpi} dpi", result.visually_identical)
        )
    else:
        rows.append(("rendered pixels", None))

    out = []
    for label, state in rows:
        word, color = mark(state)
        out.append((f"{label}: {word}", color))
    return out


def _add_summary_page(doc: "fitz.Document", result: ComparisonResult) -> None:
    page = doc.new_page(0)
    insert_at = 1
    y = 54
    page.insert_text((54, y), "Exact PDF Comparison", fontsize=20, fontname="hebo")
    y += 22
    page.insert_text(
        (54, y),
        "Every check below is an equality test on what the files contain.",
        fontsize=10,
        fontname="helv",
    )
    y += 22

    for label, name in (("A", result.pdf_a), ("B", result.pdf_b)):
        page.insert_text((54, y), f"{label}  {name[:78]}", fontsize=10, fontname="helv")
        y += 14
    y += 8

    for text, color in _verdict_text(result):
        page.insert_text((54, y), text, fontsize=11, fontname="helv", color=color)
        y += 16
    y += 12

    _swatch(page, 54, y, _GREEN, "added in B")
    y += 16
    _swatch(page, 54, y, _RED, "removed from A (boxed at its old position)")
    y += 16
    _swatch(page, 54, y, _AMBER, "edited in place")
    y += 16
    _swatch(page, 54, y, _BLUE, "moved or restyled")
    y += 16
    _swatch(page, 54, y, _VIOLET, "pixels differ with no text change to explain it")
    y += 28

    numeric = result.numeric_changes
    if numeric:
        page.insert_text(
            (54, y), f"Figures that changed ({len(numeric)})", fontsize=13,
            fontname="hebo",
        )
        y += 18
        for comparison, change in numeric:
            if y > page.rect.height - 60:
                page = doc.new_page(insert_at)
                insert_at += 1
                y = 54
            where = "" if comparison.page_b is None else f"p{comparison.page_b + 1}"
            label = change.context[:44] or change.before.text.strip()
            # Drawn with a base-14 font, so keep this line to Latin-1.
            page.insert_text(
                (54, y),
                f"{where}  {label}: {format_number(change.before.value)} -> "
                f"{format_number(change.after.value)} "
                f"({format_number(change.delta)})",
                fontsize=10,
                fontname="helv",
            )
            y += 14
        y += 10
        if y < page.rect.height - 40:
            page.insert_text(
                (54, y),
                "Labels are the nearest text on the same baseline: a reading aid.",
                fontsize=9,
                fontname="helv",
                color=(0.4, 0.4, 0.4),
            )


def write_diff_pdf(
    result: ComparisonResult, output_pdf: str, summary: bool = True
) -> str:
    """Mark up a copy of ``result.pdf_b`` with the differences and save it."""
    doc = fitz.open(result.pdf_b)
    try:
        for comparison in result.pages:
            if comparison.page_b is None or comparison.identical:
                continue
            _mark_page(doc[comparison.page_b], comparison)
        if summary:
            _add_summary_page(doc, result)
        doc.save(output_pdf, garbage=4, deflate=True)
    finally:
        doc.close()
    return output_pdf
