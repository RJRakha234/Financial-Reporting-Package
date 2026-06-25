"""Write a copy of the published PDF marked up with the whole-content comparison.

Every line whose words and numbers were all found in the HTML is highlighted
**green**; a token that differs is boxed **red**, and a token missing from the
HTML is **orange** — each with a sticky-note comment. A summary page with the
coverage figure and the list of findings is prepended.
"""

import fitz  # PyMuPDF

from .compare import CHANGED, MISSING, ADDED, ComparisonResult

_GREEN = (0.30, 0.66, 0.36)
_RED = (0.86, 0.20, 0.18)
_ORANGE = (0.95, 0.60, 0.15)
_PAD = 1.0
_SUMMARY_CAP = 500


def _collect(result: ComparisonResult):
    """page -> list of (bbox, color, note, boxed)."""
    pages: dict[int, list] = {}

    def add(page, bbox, color, note, boxed=False):
        if bbox is None or page is None:
            return
        pages.setdefault(page, []).append((bbox, color, note, boxed))

    # Green every line whose tokens all matched (whole-content coverage).
    for line in result.pdf_lines:
        if line.tokens and all(t.status == "matched" for t in line.tokens):
            add(line.page, line.bbox, _GREEN, None)

    # Mark the differences token by token, with the finding's comment.
    for f in result.findings:
        if f.kind == CHANGED:
            color, boxed = _RED, True
        elif f.kind == MISSING:
            color, boxed = _ORANGE, False
        else:
            continue  # added-in-HTML has no place on the PDF; summary only
        for n, tok in enumerate(f.pdf_tokens):
            add(f.page, tok.bbox, color, f.message() if n == 0 else None, boxed)
    return pages


def _annotate_page(page, marks):
    for bbox, color, note, boxed in marks:
        x0, top, x1, bottom = bbox
        rect = fitz.Rect(x0 - _PAD, top - _PAD, x1 + _PAD, bottom + _PAD)
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=color)
        if note:
            annot.set_info(content=note)
        annot.update()
        if boxed:
            box = page.add_rect_annot(rect)
            box.set_colors(stroke=color)
            box.set_border(width=1.0)
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
    page.insert_text((54, y), "PDF vs HTML — whole-content check", fontsize=18, fontname="hebo")
    y += 24
    page.insert_text(
        (54, y),
        f"Coverage: {result.matched_tokens} of {result.pdf_tokens} PDF tokens "
        f"({result.coverage * 100:.1f}%) matched the HTML.",
        fontsize=11, fontname="hebo",
    )
    y += 16
    page.insert_text(
        (54, y),
        f"{len(result.by_kind(CHANGED))} changed · {len(result.by_kind(MISSING))} missing "
        f"from HTML · {len(result.by_kind(ADDED))} only in HTML.",
        fontsize=11, fontname="helv",
    )
    y += 24
    _swatch(page, 54, y, _GREEN, "line fully matched the HTML")
    y += 16
    _swatch(page, 54, y, _RED, "content differs (changed)")
    y += 16
    _swatch(page, 54, y, _ORANGE, "missing from the HTML")
    y += 22

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
