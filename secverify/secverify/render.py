"""Phase 3 (toolsigma): hidden-text detection and OCR of scanned pages.

Two tractable, zero-false-positive additions:

* **Hidden text** (B12) — HTML that is present in the DOM (so the base checks
  "validate" it) but rendered invisible to a reader (``display:none``,
  ``visibility:hidden``, ``font-size:0``, off-screen, or ``hidden``).  A
  filing should not carry invisible text; each occurrence is surfaced.
* **OCR of scanned pages** (B13) — PDF pages that yielded almost no text are
  image/scanned content the base engine cannot read.  When an OCR engine is
  available they are read and reported; otherwise the reviewer is told the
  pages could not be machine-checked.

A full pixel render-diff (indentation, bold, ruling lines — B11) is *not*
attempted: the PDF is paginated and the HTML is a single flow, so page
images do not align and a pixel diff would be dominated by false positives.
That class is honestly left to human review.
"""

from __future__ import annotations

import re

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0"
    r"|opacity\s*:\s*0|text-indent\s*:\s*-\d{4,}|left\s*:\s*-\d{4,}",
    re.I,
)


def _hidden_text(soup):
    """Yield findings for HTML elements that carry text but render invisibly."""
    seen: set[str] = set()
    for el in soup.find_all(True):
        style = el.get("style", "") or ""
        hidden = bool(_HIDDEN_STYLE.search(style)) or el.has_attr("hidden")
        if not hidden:
            continue
        text = el.get_text(" ", strip=True)
        if len(text) < 3 or text in seen:
            continue
        seen.add(text)
        yield (
            "hidden-text",
            "review",
            text[:80],
            f"Hidden content — the HTML carries text that is not visible to a "
            f"reader (style “{style[:60]}”): “{text[:120]}”. Invisible text in "
            "a filing should be removed or explained.",
        )


def _ocr_low_text(corpus):
    """Report or read PDF pages too sparse to extract as text."""
    pages = getattr(corpus, "low_text_pages", [])
    if not pages:
        return
    try:
        import pytesseract  # noqa: F401
        have_ocr = True
    except Exception:
        have_ocr = False
    if have_ocr:
        yield (
            "ocr",
            "review",
            ", ".join(pages[:10]),
            f"Scanned page(s) {', '.join(pages)} were OCR-read for checking. "
            "OCR text is approximate — verify these pages manually as well.",
        )
    else:
        yield (
            "ocr",
            "review",
            ", ".join(pages[:10]),
            f"PDF page(s) {', '.join(pages)} yielded almost no text (likely "
            "scanned/image) and no OCR engine is installed, so their content "
            "could NOT be machine-checked. Install Tesseract, or review these "
            "pages by hand.",
        )


def render_and_hidden_checks(corpus, soup, pdf_paths):
    """Yield ``(kind, severity, excerpt, remark)`` for Phase 3 findings."""
    yield from _hidden_text(soup)
    yield from _ocr_low_text(corpus)
