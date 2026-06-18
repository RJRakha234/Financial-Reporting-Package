"""Extract text *lines* (with their bounding boxes) from a PDF using PyMuPDF.

Each line keeps the page index and a rectangle in the page's coordinate system,
so the highlighter -- also PyMuPDF -- can draw straight onto it with no
coordinate conversion.
"""

from dataclasses import dataclass, field

import fitz  # PyMuPDF


@dataclass
class Line:
    page: int
    rect: tuple  # (x0, y0, x1, y1) in PDF points, top-left origin
    text: str
    norm: str = ""
    status: str = "unchecked"  # "match" | "mismatch" | "unchecked"

    @property
    def fitz_rect(self):
        return fitz.Rect(*self.rect)


def extract_lines(path: str) -> list[Line]:
    """Return every text line in reading order across all pages."""
    doc = fitz.open(path)
    try:
        lines: list[Line] = []
        for pno in range(doc.page_count):
            page = doc[pno]
            # words: (x0, y0, x1, y1, "word", block_no, line_no, word_no)
            words = page.get_text("words")
            groups: dict[tuple, list] = {}
            for w in words:
                groups.setdefault((w[5], w[6]), []).append(w)

            def line_top(key):
                return min(w[1] for w in groups[key])

            for key in sorted(groups, key=line_top):
                ws = sorted(groups[key], key=lambda w: w[0])
                text = " ".join(w[4] for w in ws).strip()
                if not text:
                    continue
                x0 = min(w[0] for w in ws)
                y0 = min(w[1] for w in ws)
                x1 = max(w[2] for w in ws)
                y1 = max(w[3] for w in ws)
                lines.append(Line(page=pno, rect=(x0, y0, x1, y1), text=text))
        return lines
    finally:
        doc.close()


def page_count(path: str) -> int:
    doc = fitz.open(path)
    try:
        return doc.page_count
    finally:
        doc.close()
