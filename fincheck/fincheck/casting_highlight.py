"""Annotate the current-period PDF with the casting result.

For every line item the year-to-date (six-month) figure is the one being
verified, so it is coloured by outcome; the current-quarter figure that feeds
into it is marked as a checked component:

* **green**  — the six-month figure casts (3M current + 3M prior, within tolerance);
* **red**    — it does NOT cast (outlined, with the expected value in a note);
* **orange** — it could not be verified (the prior figure was missing);
* **yellow** — a current-quarter figure that was added into the check.

A summary page with a legend and the list of mismatches is prepended.
"""

from __future__ import annotations

import fitz  # PyMuPDF

from .casting import CastResult
from .numbers import format_number

_YELLOW = (0.99, 0.86, 0.30)
_GREEN = (0.30, 0.66, 0.36)
_RED = (0.86, 0.20, 0.18)
_ORANGE = (0.95, 0.60, 0.15)
_PAD = 1.5

_PRIORITY = {"component": 0, "ok": 1, "unverified": 2, "mismatch": 3}
_COLOR = {
    "component": _YELLOW,
    "ok": _GREEN,
    "unverified": _ORANGE,
    "mismatch": _RED,
}


def _key(cell):
    return (round(cell.x0), round(cell.top))


def _annotate_page(page, items) -> None:
    kind: dict[tuple, tuple] = {}     # key -> (priority, role, cell)
    notes: dict[tuple, str] = {}

    def put(cell, role, note=None):
        if cell is None:
            return
        key = _key(cell)
        prio = _PRIORITY[role]
        if key not in kind or prio > kind[key][0]:
            kind[key] = (prio, role, cell)
        if note:
            notes[key] = note

    for status, check in items:
        put(check.current_quarter_cell, "component")
        msg = None
        if status == "mismatch":
            msg = (
                f"Does not cast: 6M {format_number(check.six_month)} vs "
                f"3M+3M {format_number(check.expected)} "
                f"(off by {format_number(check.difference)})"
            )
        elif status == "unverified":
            msg = "Could not verify: prior-period figure not found."
        put(check.six_cell, status if status in _COLOR else "ok", msg)

    for prio, role, cell in kind.values():
        rect = fitz.Rect(cell.x0 - _PAD, cell.top - _PAD, cell.x1 + _PAD, cell.bottom + _PAD)
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=_COLOR[role])
        note = notes.get(_key(cell))
        if note:
            annot.set_info(content=note)
        annot.update()
        if role == "mismatch":
            box = page.add_rect_annot(rect)
            box.set_colors(stroke=_RED)
            box.set_border(width=1.2)
            box.update()


def _swatch(page, x, y, color, label):
    page.draw_rect(fitz.Rect(x, y - 8, x + 16, y + 2), color=color, fill=color)
    page.insert_text((x + 24, y), label, fontsize=10, fontname="helv")


def _add_summary_page(doc, result: CastResult) -> None:
    mism = result.mismatches
    ok = result.by_status("ok")
    unver = result.by_status("unverified")

    page = doc.new_page(0)
    insert_at = 1
    y = 54
    page.insert_text((54, y), "Casting Report", fontsize=20, fontname="hebo")
    y += 26
    page.insert_text(
        (54, y),
        f"Cast {len(ok) + len(mism)} figures (tolerance "
        f"±{format_number(result.tolerance)}): {len(ok)} cast correctly, "
        f"{len(mism)} do not cast"
        + (f", {len(unver)} unverified." if unver else "."),
        fontsize=11, fontname="helv",
    )
    y += 24
    _swatch(page, 54, y, _YELLOW, "current-quarter figure added into the check"); y += 16
    _swatch(page, 54, y, _GREEN, "year-to-date figure that casts"); y += 16
    _swatch(page, 54, y, _RED, "year-to-date figure that does NOT cast"); y += 16
    _swatch(page, 54, y, _ORANGE, "could not be verified"); y += 26

    for n, c in enumerate(mism, 1):
        if y > page.rect.height - 70:
            page = doc.new_page(insert_at)
            insert_at += 1
            y = 54
        page.insert_text(
            (54, y), f"{n}. Note {c.note} — {c.label.strip()[:58]} [{c.year}]",
            fontsize=11, fontname="hebo",
        )
        y += 15
        page.insert_text(
            (54, y),
            f"   6-month {format_number(c.six_month)}, "
            f"3M+3M {format_number(c.expected)} "
            f"(off by {format_number(c.difference)})",
            fontsize=10, fontname="helv", color=_RED,
        )
        y += 22


def write_highlighted_pdf(result: CastResult, output_pdf: str) -> str:
    """Write an annotated copy of the *current-period* PDF."""
    doc = fitz.open(result.current_pdf)
    by_page: dict[int, list] = {}
    for c in result.checks:
        status = c.status(result.tolerance)
        if status == "not_additive":
            continue
        by_page.setdefault(c.page_index, []).append((status, c))

    for page_index, items in by_page.items():
        if 0 <= page_index < len(doc):
            _annotate_page(doc[page_index], items)

    _add_summary_page(doc, result)
    doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    return output_pdf
