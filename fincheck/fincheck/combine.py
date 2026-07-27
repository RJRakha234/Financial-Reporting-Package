"""Join the parts of a reporting package into one benchmark document.

A filing is rarely one file. The auditor's report is signed and issued
separately from the statements it opines on, schedules arrive from a different
team, and the exhibit that goes to the regulator is all of them bound together.
Comparing the exhibit against only one part reports the rest as content the
exhibit invented — which is both wrong and alarming.

Joining them here rather than asking the reviewer to do it in Acrobat matters
for one reason: **the highlight annotations have to survive**. Those carry the
section numbers the whole comparison is anchored on, and most ways of merging
PDFs quietly drop them.

Section numbers are checked for collisions across the parts, because two files
that both number a section ``1`` would otherwise silently compare the auditor's
opinion against a balance sheet.
"""

from dataclasses import dataclass, field

import fitz  # PyMuPDF

from .marks import read_marks


@dataclass
class Combined:
    """The joined document, and where each part landed in it."""

    path: str
    parts: list = field(default_factory=list)  # (source path, first page, pages)
    collisions: list = field(default_factory=list)  # section numbers used twice

    @property
    def page_count(self) -> int:
        return sum(pages for _, _, pages in self.parts)

    def part_of(self, page: int) -> str | None:
        """Which source file a page of the combined document came from."""
        for path, first, pages in self.parts:
            if first <= page < first + pages:
                return path
        return None


def combine(sources: list[str], output_pdf: str) -> Combined:
    """Concatenate ``sources`` in order into ``output_pdf``.

    Annotations are carried across, so reviewer section numbers keep working.
    Bookmarks are rebuilt so each part is reachable by name.
    """
    if not sources:
        raise ValueError("no source PDFs to combine")

    seen: dict = {}
    collisions: list = []
    for path in sources:
        marks = read_marks(path)
        for label in (marks.labels if marks else []):
            if label in seen and seen[label] != path:
                collisions.append((label, seen[label], path))
            else:
                seen[label] = path

    out = fitz.open()
    parts: list = []
    toc: list = []
    try:
        for path in sources:
            src = fitz.open(path)
            try:
                first = out.page_count + 1
                # annots=True is the default, and is the whole point: the
                # section numbers live in the highlight comments.
                out.insert_pdf(src, annots=True)
                parts.append((path, first, src.page_count))
                name = path.rsplit("/", 1)[-1]
                toc.append([1, name[:-4] if name.lower().endswith(".pdf") else name,
                            first])
            finally:
                src.close()
        if toc:
            out.set_toc(toc)
        out.save(output_pdf, garbage=4, deflate=True)
    finally:
        out.close()

    return Combined(path=output_pdf, parts=parts, collisions=collisions)
