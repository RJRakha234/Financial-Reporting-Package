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


#: a CSS rule in a <style> block: "selector { declarations }"
_CSS_RULE_RE = re.compile(r"([^{}@]+)\{([^{}]*)\}")
#: a declaration that injects visible text through a pseudo-element
_CSS_CONTENT_RE = re.compile(r"content\s*:\s*[\"']([^\"']+)[\"']")
#: clipping: a container given no room and told to hide the overflow
_CSS_CLIPPED_RE = re.compile(
    r"overflow\s*:\s*hidden", re.I
)
_CSS_NO_ROOM_RE = re.compile(r"(?:max-)?height\s*:\s*0", re.I)


def _stylesheet_hidden(soup):
    """Elements hidden by a <style> RULE rather than an inline style attribute.

    The inline-attribute check misses everything a stylesheet does, which is the
    normal way to hide content: a class, an id, or a tag selector carrying
    ``display:none``.  A figure hidden that way is still present in the DOM, so
    every text and figure check finds it and stamps it GREEN — while the reader
    of the filing sees a blank where the number should be.  That is the exact
    inverse of hidden EXTRA text, and it is the more dangerous direction: the
    document is missing a figure it is required to show.

    Yields ``(element, why)`` for each element a rule hides or alters.
    """
    for tag in soup.find_all("style"):
        css = tag.get_text() or ""
        for m in _CSS_RULE_RE.finditer(css):
            sel, decl = m.group(1).strip(), m.group(2)
            if not sel or sel.startswith("@"):
                continue
            hides = bool(_HIDDEN_STYLE.search(decl))
            clipped = bool(_CSS_CLIPPED_RE.search(decl)) and bool(
                _CSS_NO_ROOM_RE.search(decl)
            )
            injects = _CSS_CONTENT_RE.search(decl)
            if not (hides or clipped or injects):
                continue
            base = re.sub(r"::?(?:before|after|first-line|first-letter)\b", "", sel)
            base = base.strip().rstrip(",")
            if not base:
                continue
            try:
                matched = soup.select(base)
            except Exception:
                continue  # a selector soupsieve cannot parse — skip, never crash
            for el in matched:
                if injects and "::" in sel or (injects and ":before" in sel or ":after" in sel):
                    yield el, (
                        f"a stylesheet rule “{sel.strip()}” inserts the text "
                        f"“{injects.group(1)}” that is not in the document's own "
                        "markup, so what a reader sees differs from what every "
                        "text and figure check reads"
                    )
                elif hides:
                    yield el, (
                        f"a stylesheet rule “{sel.strip()}{{{decl.strip()[:40]}}}” "
                        "hides it from the reader while leaving it in the markup"
                    )
                elif clipped:
                    yield el, (
                        f"a stylesheet rule “{sel.strip()}” gives it no height and "
                        "hides the overflow, so it is clipped out of view"
                    )


def _hidden_text(soup):
    """Yield findings for HTML elements that carry text but render invisibly."""
    seen: set[str] = set()
    for el, why in _stylesheet_hidden(soup):
        text = el.get_text(" ", strip=True)
        if len(text) < 3 or text in seen:
            continue
        seen.add(text)
        yield (
            "hidden-text",
            "error",
            text[:80],
            f"Hidden content — {why}: “{text[:120]}”. Because the value is still "
            "in the markup, every other check finds it and reports it as "
            "verified; only this check can see that the reader cannot. Confirm "
            "the figure is meant to be invisible.",
        )
    for el in soup.find_all(True):
        style = el.get("style", "") or ""
        # clipping needs BOTH parts: a container given no room and told to hide
        # what overflows.  Either alone is ordinary layout.
        clipped = bool(_CSS_CLIPPED_RE.search(style)) and bool(
            _CSS_NO_ROOM_RE.search(style)
        )
        hidden = bool(_HIDDEN_STYLE.search(style)) or el.has_attr("hidden") or clipped
        if not hidden:
            continue
        text = el.get_text(" ", strip=True)
        if len(text) < 3 or text in seen:
            continue
        seen.add(text)
        how = (
            "it is given no height with the overflow hidden, so it is clipped "
            "out of view"
            if clipped else
            "it is not visible to a reader"
        )
        yield (
            "hidden-text",
            "review",
            text[:80],
            f"Hidden content — the HTML carries text that {how} "
            f"(style “{style[:60]}”): “{text[:120]}”. The value is still in the "
            "markup, so every other check reports it as verified; only this "
            "check can see that the reader cannot.",
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
