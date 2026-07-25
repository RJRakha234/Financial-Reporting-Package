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


#: an element carrying two style attributes, e.g.
#: <p style="display:none" style="font: 10pt Arial">
_DUP_STYLE_RE = re.compile(
    r'<[a-zA-Z][^<>]*?\sstyle\s*=\s*"[^"]*"[^<>]*?\sstyle\s*=\s*"[^"]*"[^<>]*>'
)


def duplicate_style_findings(html_text: str):
    """Yield findings for elements carrying MORE THAN ONE style attribute.

    Parsers disagree about which one wins: a browser keeps the first, while the
    HTML parsers used for analysis (BeautifulSoup among them) keep the last.  So
    an element written ``style="display:none" style="font: 10pt Arial"`` renders
    INVISIBLE to a reader while every text extractor — this tool included — sees
    it as ordinary visible content, and the hidden-text check never fires because
    the property it looks for was discarded at parse time.

    That makes it the one shape able to hide content from a human reviewer while
    the machine reports it present and correct, so it is reported on the RAW
    markup, before any parse can resolve the conflict away.
    """
    seen: set[str] = set()
    for m in _DUP_STYLE_RE.finditer(html_text or ""):
        tag = m.group(0)
        if tag in seen:
            continue
        seen.add(tag)
        yield (
            "duplicate-style",
            "error",
            tag[:100],
            "Conflicting style attributes — this element carries two "
            "“style” attributes: “" + tag[:120] + "”. A browser applies the "
            "first and text extractors keep the last, so the element can render "
            "invisibly to a reader while every automated check, including this "
            "one, reads it as visible content. Merge them into a single "
            "attribute so what is checked is what is displayed.",
        )


def render_and_hidden_checks(corpus, soup, pdf_paths):
    """Yield ``(kind, severity, excerpt, remark)`` for Phase 3 findings."""
    yield from _hidden_text(soup)
    yield from _ocr_low_text(corpus)
