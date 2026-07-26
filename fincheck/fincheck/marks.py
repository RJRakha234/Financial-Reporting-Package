"""Read section numbers a reviewer has marked on a PDF.

Content matching is a similarity judgement, and a judgement can decline: two
passages that plainly correspond may fall below the floor and be reported as
present in one document only — a blank where a comparison should have been. The
reviewer who can see that they correspond has no way to say so.

This is that way. Highlight a passage in any PDF reader, type a number in the
comment, and put the same number on the counterpart in the other document.
Those numbers become hard constraints: content marked ``5`` is compared against
content marked ``5``, whatever the similarity score says, and a section numbered
in both documents can never come out one-sided.

Nothing about it is required — an unmarked pair still compares as before — and
marking part of a document is fine, since anything unmarked simply falls back to
content matching.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

import fitz  # PyMuPDF

# Vertical slack when testing whether a row sits inside a marked region: a
# highlight is drawn around the glyphs, so a descender can poke out below it.
_CONTAINMENT_SLACK = 3.0


@dataclass(frozen=True)
class Mark:
    """One highlighted region and the section number written on it."""

    label: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    def contains(self, page: int, bbox: tuple) -> bool:
        if page != self.page:
            return False
        x0, y0, x1, y1 = bbox
        middle = (y0 + y1) / 2
        if not (self.y0 - _CONTAINMENT_SLACK <= middle <= self.y1 + _CONTAINMENT_SLACK):
            return False
        # Any horizontal overlap counts: a highlight covering a wrapped line
        # need not span the full column.
        return x0 <= self.x1 and x1 >= self.x0


@dataclass
class Marks:
    """Every numbered region found in one document."""

    by_label: dict = field(default_factory=dict)

    @property
    def labels(self) -> list[str]:
        return sorted(self.by_label, key=_sort_key)

    def __bool__(self) -> bool:
        return bool(self.by_label)

    def label_for(self, page: int, bbox: tuple) -> str | None:
        """Which numbered section this row falls in, if any."""
        best, best_area = None, 0.0
        for label, regions in self.by_label.items():
            for mark in regions:
                if mark.contains(page, bbox):
                    # Nested or overlapping marks: the tightest one wins, so a
                    # sub-section marked inside a larger one takes precedence.
                    area = (mark.x1 - mark.x0) * (mark.y1 - mark.y0)
                    if best is None or area < best_area:
                        best, best_area = label, area
        return best


def _sort_key(label: str):
    """Order 2 before 10, and keep anything non-numeric after the numbers."""
    parts = re.findall(r"\d+|\D+", label)
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def read_marks(pdf_path: str) -> Marks:
    """Collect numbered highlight annotations from a PDF.

    Any annotation carrying a comment is taken as a section marker, keyed by that
    comment. Several regions may share a number — a section broken over a page
    boundary needs one highlight per page — and they are treated as one section.
    """
    doc = fitz.open(pdf_path)
    try:
        found = defaultdict(list)
        for index, page in enumerate(doc):
            for annot in page.annots() or []:
                label = (annot.info.get("content") or "").strip()
                if not label:
                    continue
                rect = annot.rect
                found[label].append(
                    Mark(
                        label=label,
                        page=index + 1,
                        x0=round(rect.x0, 2),
                        y0=round(rect.y0, 2),
                        x1=round(rect.x1, 2),
                        y1=round(rect.y1, 2),
                    )
                )
        return Marks(by_label=dict(found))
    finally:
        doc.close()
