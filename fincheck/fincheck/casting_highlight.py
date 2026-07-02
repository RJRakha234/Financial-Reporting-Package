"""Annotate the current-period PDF with the casting result.

Both figures that make up a successful cast are coloured **green**, so every
number that has been cast and agrees is green at a glance:

* **green**  — a figure that casts (the six-month total and the current-quarter
  figure that feeds it, when 3M current + 3M prior agrees within tolerance);
* **red**    — a six-month figure that does NOT cast (also outlined);
* **orange** — could not be verified, or a figure feeding a failed cast.

The colours are baked into the pages (not left as live annotations) so the
marked-up PDF opens and scrolls as quickly as the original — a filing can carry
a thousand marks. The legend and the list of mismatches are on the prepended
summary page, so no per-cell pop-up notes are needed.

(The third figure in each cast — the prior quarter — lives in the *other* PDF,
so it is reported in the Excel/HTML rather than highlighted here.)
"""

from __future__ import annotations

import fitz  # PyMuPDF

from .casting import CastResult
from .numbers import format_number

_GREEN = (0.30, 0.66, 0.36)
_RED = (0.86, 0.20, 0.18)
_ORANGE = (0.95, 0.60, 0.15)
_PAD = 1.5

_PRIORITY = {"ok": 0, "feeds_fail": 1, "unverified": 2, "mismatch": 3}
_COLOR = {"ok": _GREEN, "feeds_fail": _ORANGE, "unverified": _ORANGE, "mismatch": _RED}


def _key(cell):
    return (round(cell.x0), round(cell.top))


def _collect(result: CastResult):
    """Return {page_index: {cell_key: (role, cell)}} resolving by priority."""
    pages: dict[int, dict[tuple, tuple]] = {}

    def put(page_index, cell, role):
        if cell is None or page_index is None:
            return
        bucket = pages.setdefault(page_index, {})
        key = _key(cell)
        if key not in bucket or _PRIORITY[role] > _PRIORITY[bucket[key][0]]:
            bucket[key] = (role, cell)

    for c in result.checks:
        status = c.status(result.tolerance)
        if status == "not_additive":
            continue
        q_page = c.quarter_page_index if c.quarter_page_index is not None else c.page_index
        if status == "ok":
            put(c.page_index, c.six_cell, "ok")
            put(q_page, c.current_quarter_cell, "ok")
        elif status == "mismatch":
            put(c.page_index, c.six_cell, "mismatch")
            put(q_page, c.current_quarter_cell, "feeds_fail")
        else:  # unverified
            put(c.page_index, c.six_cell, "unverified")
    return pages


def _annotate(page, cells) -> None:
    for role, cell in cells.values():
        rect = fitz.Rect(cell.x0 - _PAD, cell.top - _PAD,
                         cell.x1 + _PAD, cell.bottom + _PAD)
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=_COLOR[role])
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
    _swatch(page, 54, y, _GREEN, "figure that casts (6M = 3M current + 3M prior)"); y += 16
    _swatch(page, 54, y, _RED, "six-month figure that does NOT cast"); y += 16
    _swatch(page, 54, y, _ORANGE, "could not be verified / feeds a failed cast"); y += 26

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
    # Real filings sometimes carry a slightly malformed xref that MuPDF repairs
    # while printing noisy warnings; silence those — the repaired copy is fine.
    fitz.TOOLS.mupdf_display_errors(False)
    doc = fitz.open(result.current_pdf)
    for page_index, cells in _collect(result).items():
        if 0 <= page_index < len(doc):
            _annotate(doc[page_index], cells)
    # Flatten the highlights into the page content so the viewer has no live
    # annotation objects to manage — a filing can carry a thousand marks, and
    # that is what makes an annotated PDF slow to open and scroll.
    if hasattr(doc, "bake"):
        doc.bake()
    _add_summary_page(doc, result)
    # Light cleanup only: full garbage collection with deflation re-compresses
    # every stream and can take tens of seconds; garbage=1 saves near-instantly.
    doc.save(output_pdf, garbage=1)
    doc.close()
    return output_pdf
