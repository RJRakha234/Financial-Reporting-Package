"""Phase 3 (toolsigma): images — the one place the text checks cannot see.

A number or word rendered inside an image (a chart, a scanned signature, a
currency symbol shipped as a GIF) is invisible to every text/figure check, so
it could differ from the PDF without any other detector noticing.  Rather than
leave that silent, this module makes images first-class citizens of the report:

* **Inventory** — every ``<img>`` in the HTML is counted and grouped by file
  name / alt text, and surfaced as ONE review item per document, so a reviewer
  always knows how much of the exhibit lives outside the machine-checkable
  text.  (Real SEC exhibits render the ₹ symbol as ``rupee-symbol.gif`` —
  dozens of currency marks the text-based currency check cannot see.)
* **OCR of embedded images** — an image embedded as a ``data:`` URI is decoded
  and read with Tesseract when available.  Significant figures found in the
  image that appear nowhere in the PDF are flagged for review (OCR is
  approximate, so this is never a hard error).
* **PDF-side images** — pages of the reference PDFs carrying embedded images
  are reported, since a chart's figures cannot be text-verified either.

Everything here is ``review`` severity: an image finding is a prompt to look,
never a verdict.
"""

from __future__ import annotations

import base64
import io
import re
from collections import Counter

from .numbers import is_significant, iter_tokens


def _ocr_engine():
    """Return the pytesseract module when a working engine exists, else None."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return pytesseract
    except Exception:
        return None


def _decode_data_uri(src: str):
    """PIL image from a ``data:image/...;base64,`` URI, or None."""
    m = re.match(r"data:image/[^;]+;base64,(.*)", src, re.S)
    if not m:
        return None
    try:
        from PIL import Image

        return Image.open(io.BytesIO(base64.b64decode(m.group(1))))
    except Exception:
        return None


def _ocr_text(pytesseract, pil_img) -> str:
    """OCR *pil_img*, upscaling small images so Tesseract can read them."""
    try:
        img = pil_img.convert("L")
        w, h = img.size
        if h < 60:
            scale = max(2, 80 // max(1, h))
            img = img.resize((w * scale, h * scale))
        return pytesseract.image_to_string(img) or ""
    except Exception:
        return ""


def _group_key(img) -> tuple[str, str]:
    src = (img.get("src") or "").strip()
    name = "embedded-image" if src.startswith("data:") else (
        src.rsplit("/", 1)[-1][:60] or "(no src)"
    )
    alt = (img.get("alt") or "").strip()
    return name, (alt[:40] or "(no alt)")


def image_checks(corpus, soup, pdf_paths):
    """Yield ``(kind, severity, excerpt, remark)`` for image findings."""
    imgs = soup.find_all("img")
    ocr = _ocr_engine()

    if imgs:
        groups = Counter(_group_key(i) for i in imgs)
        desc = "; ".join(
            f"{n}× {name}"
            + (f" (alt “{alt}”)" if alt != "(no alt)" else ", no alt text")
            for (name, alt), n in groups.most_common()
        )
        yield (
            "image",
            "review",
            f"{len(imgs)} image(s): {desc[:130]}",
            f"Images — the HTML renders {len(imgs)} image(s): {desc}. Image "
            "content is not text, so NO text or figure check can see it — e.g. "
            "a ₹ symbol shipped as a GIF is invisible to the currency check, "
            "and a chart's figures cannot be validated. Verify each image "
            "displays the intended symbol/graphic"
            + ("." if ocr else " (no OCR engine installed to read embedded images)."),
        )

        if ocr:
            seen: set[str] = set()
            for img in imgs:
                src = img.get("src") or ""
                if not src.startswith("data:") or src in seen:
                    continue
                seen.add(src)
                pil = _decode_data_uri(src)
                if pil is None:
                    continue
                text = " ".join(_ocr_text(ocr, pil).split())
                if not text:
                    continue
                unmatched = []
                for _s, _e, tok, key in iter_tokens(text):
                    if is_significant(key, tok) and not corpus.has_number(key):
                        unmatched.append(tok)
                if unmatched:
                    yield (
                        "image-ocr",
                        "review",
                        f"embedded image reads “{text[:80]}”",
                        f"Image content — OCR read “{text[:160]}” inside an "
                        f"embedded image, and the figure(s) "
                        f"{', '.join(unmatched[:6])} were not found in the PDF. "
                        "OCR is approximate — verify this image against the PDF "
                        "by eye.",
                    )

    # PDF-side embedded images: their content is equally un-checkable.
    pages_with_images: list[str] = []
    if pdf_paths:
        try:
            import pdfplumber
            from pathlib import Path

            for path in pdf_paths:
                with pdfplumber.open(path) as pdf:
                    for no, page in enumerate(pdf.pages, start=1):
                        if page.images:
                            pages_with_images.append(f"{Path(path).stem} p.{no}")
        except Exception:
            pages_with_images = []
    if pages_with_images:
        shown = ", ".join(pages_with_images[:8])
        more = "" if len(pages_with_images) <= 8 else f" (+{len(pages_with_images)-8} more)"
        yield (
            "image",
            "review",
            f"PDF pages with embedded images: {shown}{more}",
            f"PDF images — page(s) {shown}{more} of the reference PDF(s) carry "
            "embedded image(s) (charts, logos, scans). Any figures inside them "
            "cannot be text-verified against the HTML; compare those regions "
            "by eye.",
        )
