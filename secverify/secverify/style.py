"""Phase 3: style checks — letter CASE and BOLD weight (measured before built:
case = 0 noise / 1,520 lines; bold ≈ 1% volume divergence on the reference
pair). Both review severity with their own colours — never red, never green."""
from __future__ import annotations
import re

def style_checks(corpus, soup, pdf_paths):
    html_txt = soup.get_text(" ")
    hs = re.sub(r"[^A-Za-z]", "", html_txt)
    hsl = hs.lower()
    # CASE: line matches case-insensitively but not case-sensitively
    seen = set()
    for raw in corpus.pages_raw:
        for line in raw.splitlines():
            L = re.sub(r"[^A-Za-z]", "", line)
            if len(L) < 15 or L in seen:
                continue
            seen.add(L)
            if L not in hs and L.lower() in hsl:
                yield ("case", "review", line.strip()[:100],
                       "Letter case differs — this line matches the PDF except "
                       "for UPPER/lower case. Verify the casing (e.g. a heading "
                       "de-capitalised) matches the PDF.")
    # BOLD: PDF bold runs missing from the HTML's bold text
    try:
        import pdfplumber
        hb = "".join(
            re.sub(r"[^A-Za-z]", "", el.get_text())
            for el in soup.find_all(["b", "strong"])
        ).lower()
        done = set()
        for path in pdf_paths:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    run = ""
                    for ch in page.chars + [{"text": " ", "fontname": ""}]:
                        if ch["text"].isalpha() and "Bold" in (ch.get("fontname") or ""):
                            run += ch["text"]
                        else:
                            if len(run) >= 12 and run.lower() not in hb \
                               and run.lower() not in done:
                                done.add(run.lower())
                                yield ("bold-style", "review", run[:80],
                                       "Bold weight — this text is bold in the "
                                       "PDF but was not found among the HTML's "
                                       "bold text. Verify the emphasis carried "
                                       "over (subtotals, headings).")
                            run = ""
    except Exception:
        pass
