"""Write a highlighted copy of the current filing for rollforward results.

Each comparative figure that was checked is marked in the original PDF:

* **green** — the comparative agrees with the previously published figure;
* **red**   — it does not (outlined, with a note giving the published value).

A summary page listing every mismatch is prepended. PyMuPDF shares pdfplumber's
coordinate system, so cell boxes map straight onto page rectangles.
"""

import fitz  # PyMuPDF

from .numbers import format_number

_GREEN = (0.30, 0.66, 0.36)
_RED = (0.86, 0.20, 0.18)
_ORANGE = (0.95, 0.60, 0.15)
_PAD = 1.5


def _annotate_matrix(page, rows) -> None:
    """Mark the first cell of each reconciled matrix row (green) or review (orange)."""
    for c in rows:
        cell = c.cell
        rect = fitz.Rect(cell.x0 - _PAD, cell.top - _PAD, cell.x1 + _PAD,
                         cell.bottom + _PAD)
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=_GREEN if c.status == "ok" else _ORANGE)
        annot.set_info(content=(
            f"note {c.section} · {c.metric.strip()} ({c.period_desc}): "
            + ("matches the previously published figures."
               if c.status == "ok" else "could not be reconciled — review.")))
        annot.update()


def _annotate_page(page, checks) -> None:
    # If a cell is both ok and mismatch across priors, the mismatch wins.
    chosen: dict[tuple, object] = {}
    for c in checks:
        cell = c.current.cell
        key = (round(cell.x0), round(cell.top))
        if key not in chosen or (
            c.status == "mismatch" and chosen[key].status == "ok"
        ):
            chosen[key] = c

    for c in chosen.values():
        cell = c.current.cell
        x0, top, x1, bottom = cell.bbox
        rect = fitz.Rect(x0 - _PAD, top - _PAD, x1 + _PAD, bottom + _PAD)
        annot = page.add_highlight_annot(rect)
        if c.status == "mismatch":
            annot.set_colors(stroke=_RED)
            annot.set_info(
                content=(
                    f"Rollforward mismatch: current {format_number(c.current.value)}, "
                    f"published {format_number(c.prior.value)} "
                    f"(off by {format_number(c.difference)})."
                )
            )
            annot.update()
            box = page.add_rect_annot(rect)
            box.set_colors(stroke=_RED)
            box.set_border(width=1.2)
            box.update()
        else:
            annot.set_colors(stroke=_GREEN)
            annot.update()


def _swatch(page, x, y, color, label):
    page.draw_rect(fitz.Rect(x, y - 8, x + 16, y + 2), color=color, fill=color)
    page.insert_text((x + 24, y), label, fontsize=10, fontname="helv")


def _add_summary_page(doc, checks) -> None:
    mism = [c for c in checks if c.status == "mismatch"]
    ok = [c for c in checks if c.status == "ok"]

    page = doc.new_page(0)
    insert_at = 1
    y = 54
    page.insert_text((54, y), "Rollforward Check Report", fontsize=20, fontname="hebo")
    y += 26
    page.insert_text(
        (54, y),
        f"Checked {len(checks)} comparative figures against previously "
        "published filings.",
        fontsize=11,
        fontname="helv",
    )
    y += 16
    page.insert_text(
        (54, y),
        f"{len(mism)} do not match · {len(ok)} match.",
        fontsize=11,
        fontname="helv",
    )
    y += 26
    _swatch(page, 54, y, _GREEN, "comparative matches the published figure")
    y += 16
    _swatch(page, 54, y, _RED, "comparative does NOT match what was published")
    y += 28

    for n, c in enumerate(mism, 1):
        if y > page.rect.height - 70:
            page = doc.new_page(insert_at)
            insert_at += 1
            y = 54
        page.insert_text(
            (54, y),
            f"{n}. {c.current.period.describe()} — {c.current.label.strip()[:50]}",
            fontsize=11,
            fontname="hebo",
        )
        y += 15
        page.insert_text(
            (54, y),
            f"   current {format_number(c.current.value)}, published "
            f"{format_number(c.prior.value)} (off by {format_number(c.difference)})",
            fontsize=10,
            fontname="helv",
            color=_RED,
        )
        y += 22


def write_rollforward_pdf(
    source_pdf: str, output_pdf: str, checks, matrix_rows=()
) -> str:
    doc = fitz.open(source_pdf)
    by_page: dict[int, list] = {}
    for c in checks:
        by_page.setdefault(c.current.page_index, []).append(c)
    for page_index, page_checks in by_page.items():
        _annotate_page(doc[page_index], page_checks)

    matrix_by_page: dict[int, list] = {}
    for c in matrix_rows:
        matrix_by_page.setdefault(c.page_index, []).append(c)
    for page_index, rows in matrix_by_page.items():
        _annotate_matrix(doc[page_index], rows)

    _add_summary_page(doc, checks)
    doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()
    return output_pdf
