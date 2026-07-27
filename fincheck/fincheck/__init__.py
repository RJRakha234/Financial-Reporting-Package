"""fincheck — check totals in a financial statement, or compare two PDFs exactly.

Public API::

    from fincheck import analyze, compare
    result = analyze("statements.pdf", output_pdf="highlighted.pdf")
    print(result.consistent, result.issues)

    diff = compare("draft.pdf", "final.pdf", output_pdf="diff.pdf")
    print(diff.identical, diff.numeric_changes)
"""

from dataclasses import dataclass, field

from .align import (
    align,
    align_by_derived_sections,
    align_by_section,
    derive_sections,
    flag_moved,
    page_sections,
    group,
    rescue_moved,
    summarise,
    units_of,
)
from .blocks import segment
from .marks import _sort_key as _label_order, read_marks
from .checks import Inconsistency, TotalCheck, run_checks
from .compare import DEFAULT_DPI, ComparisonResult, SpanChange, compare_pdfs
from .diffmark import write_diff_pdf
from .extract import extract_pages
from .highlight import write_highlighted_pdf
from .report import to_dict, to_json
from .sidebyside import Meta, default_label, write_side_by_side
from .console import write_console
from .ledger import Ledger, reconcile
from .sidemarks import coverage, render_previews, write_marked_copies

__all__ = [
    "analyze",
    "AnalysisResult",
    "Inconsistency",
    "TotalCheck",
    "compare",
    "ComparisonResult",
    "SpanChange",
    "side_by_side",
    "SideBySideResult",
]


@dataclass
class AnalysisResult:
    source_pdf: str
    issues: list[Inconsistency]
    checks: list[TotalCheck] = field(default_factory=list)
    output_pdf: str | None = None

    @property
    def consistent(self) -> bool:
        return not self.issues

    @property
    def totals_checked(self) -> int:
        return sum(1 for c in self.checks if c.status in ("ok", "error"))

    @property
    def figures_checked(self) -> int:
        return sum(len(c.component_cells) for c in self.checks)

    @property
    def unverified(self) -> int:
        return sum(1 for c in self.checks if c.status == "unverified")

    def as_dict(self) -> dict:
        return to_dict(self.issues)

    def as_json(self) -> str:
        return to_json(self.issues)


def analyze(
    source_pdf: str,
    output_pdf: str | None = None,
    tolerance: float = 1.0,
    show_components: bool = True,
) -> AnalysisResult:
    """Analyse a financial-statement PDF and optionally write a highlighted copy.

    Args:
        source_pdf: path to the input PDF.
        output_pdf: if given, write an annotated PDF here.
        tolerance: absolute rounding slack allowed before a total is flagged.
        show_components: highlight every figure summed into a total (coverage).
    """
    pages = extract_pages(source_pdf)
    issues, checks = run_checks(pages, base_tolerance=tolerance)
    written = None
    if output_pdf is not None:
        written = write_highlighted_pdf(
            source_pdf, output_pdf, checks, show_components=show_components
        )
    return AnalysisResult(
        source_pdf=source_pdf, issues=issues, checks=checks, output_pdf=written
    )


def compare(
    pdf_a: str,
    pdf_b: str,
    output_pdf: str | None = None,
    dpi: int | None = DEFAULT_DPI,
    position_tolerance: float = 0.0,
    align_pages: bool = False,
) -> ComparisonResult:
    """Compare two PDFs exactly and optionally write a marked-up copy of ``pdf_b``.

    The comparison makes no layout assumptions: it tests equality of the bytes,
    the page geometry, every text span the file draws (string, font, size,
    colour, position), every vector path and image, and the rendered pixels.

    Args:
        pdf_a: the reference PDF.
        pdf_b: the PDF being checked against it.
        output_pdf: if given, write a marked-up copy of ``pdf_b`` here.
        dpi: resolution for the rendered-pixel check; ``None`` skips it.
        position_tolerance: coordinate slack; ``0`` compares positions exactly.
        align_pages: match pages by content rather than by position.
    """
    result = compare_pdfs(
        pdf_a,
        pdf_b,
        dpi=dpi,
        position_tolerance=position_tolerance,
        align_pages=align_pages,
    )
    if output_pdf is not None:
        write_diff_pdf(result, output_pdf)
    return result


@dataclass
class SideBySideResult:
    """Content-matched comparison of two documents. Structure is inferred."""

    pdf_a: str
    pdf_b: str
    sections: list
    pairs: list
    summary: object
    output_html: str | None = None
    console_html: str | None = None
    marked_sections: list = field(default_factory=list)
    marked_pdfs: list = field(default_factory=list)
    # Alignment-independent figure reconciliation, or None if not run.
    ledger: object = None

    @property
    def changed_sections(self) -> list:
        return [s for s in self.sections if s.status == "changed"]

    def figure_changes(self) -> list:
        """Every table cell whose value differs, with both sides' context."""
        return [
            (pair, pair.changed_figures)
            for section in self.sections
            for pair in section.pairs
            if pair.changed_figures
        ]


def _relative_to(target: str, html_path: str) -> str:
    """Link the report to a copy beside it, so the pair can be moved together."""
    import os

    try:
        return os.path.relpath(target, os.path.dirname(os.path.abspath(html_path)))
    except ValueError:  # different drives on Windows
        return target


def _preview_quality(total_pages: int) -> dict:
    """Preview resolution for the embedded page images, by document length.

    The image must render *larger* than the panel that shows it, or the browser
    displays it about one-to-one and it looks soft — which is what "not crisp"
    actually meant. At these resolutions the page is downscaled into the panel,
    which is sharp, and there is real detail left to magnify when the reader
    zooms in. Long documents step down, because a report still has to travel as
    one file, and the PDF link in the panel serves anyone who needs more.
    """
    if total_pages <= 24:
        return {"dpi": 150, "quality": 65}
    if total_pages <= 48:
        return {"dpi": 132, "quality": 58}
    if total_pages <= 90:
        return {"dpi": 118, "quality": 50}
    return {"dpi": 100, "quality": 45}


def _previews(copies, index: int, doc_a, doc_b, dpi: int | None) -> dict:
    """Rendered pages for the in-report preview, or nothing if switched off."""
    if not copies:
        return {}
    settings = _preview_quality(doc_a.page_count + doc_b.page_count)
    if dpi is not None:
        if dpi <= 0:
            return {}
        settings["dpi"] = dpi
    return render_previews(copies[index], **settings)


def _one_sided(pairs) -> int:
    return sum(1 for p in pairs if p.a is None or p.b is None)


def _clear(units_a, units_b):
    for unit in units_a + units_b:
        unit.section = None


def _best_alignment(units_a, units_b):
    """Section an unmarked pair the way that leaves fewest passages stranded.

    Sectioning is what makes a comparison readable — content numbered *n* is
    compared only against content numbered *n*, so it cannot come out against a
    blank — but with no reviewer marks the sections have to come from
    somewhere, and the obvious sources both fail in their own way. Headings
    detected in one document and missed in the other shift every section after
    them. Page boundaries never fail to be found, but a benchmark set as one
    enormous page gives one enormous section.

    So all three candidates are computed and the one leaving fewest passages
    without a counterpart wins. Each takes about a second.

    Pages usually win, and by a wide margin: on a 41-page filing against its
    28-page HTML conversion, page sections left nothing stranded where headings
    left 363 passages facing a blank.
    """
    def labelling() -> dict:
        return {id(u): u.section for u in units_a + units_b}

    # Rank breaks ties: a sectioned result reads better than a flat one, and
    # pages never fail to be found where a heading can be missed.
    _clear(units_a, units_b)
    plain = align(units_a, units_b)
    candidates = [(_one_sided(plain), 2, plain, labelling())]

    _clear(units_a, units_b)
    if page_sections(units_a, units_b) > 1:
        paged = align_by_section(units_a, units_b)
        candidates.append((_one_sided(paged), 0, paged, labelling()))

    _clear(units_a, units_b)
    if derive_sections(units_a) and derive_sections(units_b):
        headed = align_by_derived_sections(units_a, units_b)
        candidates.append((_one_sided(headed), 1, headed, labelling()))

    _, _, pairs, labels = min(candidates, key=lambda c: (c[0], c[1]))
    # The report reads unit.section back off the units, so restore whichever
    # labelling the winner was computed under.
    for unit in units_a + units_b:
        unit.section = labels.get(id(unit))
    return pairs


def side_by_side(
    pdf_a: str,
    pdf_b: str,
    output_html: str | None = None,
    label_a: str | None = None,
    label_b: str | None = None,
    use_marks: bool = True,
    marked_pdf_a: str | None = None,
    marked_pdf_b: str | None = None,
    auto_sections: bool = True,
    figure_ledger: bool = True,
    console_html: str | None = None,
    preview_dpi: int | None = None,
) -> SideBySideResult:
    """Match two documents paragraph by paragraph and row by row.

    Unlike :func:`compare`, this reconstructs paragraphs and tables from the
    page geometry and pairs them by similarity, so its output is a reviewer's
    worksheet rather than proof. Use :func:`compare` when you need certainty.

    Args:
        pdf_a: the benchmark — the document treated as the source of truth.
            Shown on the left and named as the benchmark throughout the report;
            every deviation is stated relative to it.
        pdf_b: the PDF verified against the benchmark, shown on the right.
        output_html: if given, write the side-by-side page here.
        label_a: column heading for the first document. Defaults to how the PDF
            was produced ("Excel export", "HTML print"), which is usually how
            people refer to these files, and to the filename otherwise.
        label_b: column heading for the second document.
        use_marks: honour section numbers a reviewer has written into highlight
            comments, matching content marked ``5`` against content marked ``5``
            rather than trusting similarity. On by default; it does nothing to a
            document with no such marks.
        marked_pdf_a: if given, write a copy of ``pdf_a`` with every compared
            section outlined and stamped with its number from the report.
        marked_pdf_b: the same for ``pdf_b``.
        auto_sections: when neither PDF carries reviewer marks, cut both at
            their own headings and compare section against matching section.
            This is how a reviewer marks these documents by hand, so the output
            matches whether or not anyone has been through them first.
        preview_dpi: resolution for the page images embedded in the report.
            ``None`` picks one from the document length; ``0`` embeds none,
            which makes a much smaller file whose cells still link out to the
            annotated PDFs.
        figure_ledger: also reconcile every printed figure in the two files as
            plain multisets, independent of the pairing. The alignment says
            *where* things differ; this says whether any figure was lost or
            invented, and cannot be misled by a mis-paired row.
    """
    marks_a = read_marks(pdf_a) if use_marks else None
    marks_b = read_marks(pdf_b) if use_marks else None
    sectioned = bool(marks_a) and bool(marks_b)

    units_a = units_of(segment(pdf_a), marks_a)
    units_b = units_of(segment(pdf_b), marks_b)

    if sectioned:
        pairs = align_by_section(units_a, units_b)
    elif auto_sections:
        pairs = _best_alignment(units_a, units_b)
    else:
        pairs = align(units_a, units_b)
    # A relocated section pairs up by content even though the order-preserving
    # alignment could not reach across; it is then flagged rather than shown
    # as a removal here and an unrelated addition there.
    pairs = rescue_moved(pairs)
    sections = group(pairs)
    summary = summarise(pairs, units_a, units_b)
    summary.moved = flag_moved(sections)
    shared_marks = (
        sorted(set(marks_a.labels) & set(marks_b.labels), key=_label_order)
        if sectioned
        else []
    )

    led = reconcile(pdf_a, pdf_b) if figure_ledger else None

    # An HTML filing has no pages to annotate. The report still links its
    # rows; there is simply no marked-up copy of that side to link into.
    from .blocks import is_html

    if is_html(pdf_a) or is_html(pdf_b):
        marked_pdf_a = marked_pdf_b = None

    copies = None
    if marked_pdf_a and marked_pdf_b:
        copies = write_marked_copies(
            pdf_a, pdf_b, sections, marked_pdf_a, marked_pdf_b
        )

    written = None
    console = None
    if output_html is not None:
        import fitz

        blank = fitz.open()
        blank.new_page()
        doc_a = blank if is_html(pdf_a) else fitz.open(pdf_a)
        doc_b = blank if is_html(pdf_b) else fitz.open(pdf_b)
        try:
            producer_a = doc_a.metadata.get("producer") or ""
            producer_b = doc_b.metadata.get("producer") or ""
            creator_a = doc_a.metadata.get("creator") or ""
            creator_b = doc_b.metadata.get("creator") or ""
            meta = Meta(
                pdf_a=pdf_a,
                pdf_b=pdf_b,
                pages_a=doc_a.page_count,
                pages_b=doc_b.page_count,
                summary=summary,
                label_a=label_a or default_label(pdf_a, producer_a, creator_a),
                label_b=label_b or default_label(pdf_b, producer_b, creator_b),
                producer_a=producer_a,
                producer_b=producer_b,
                marked_sections=len(shared_marks),
                marked_href_a=_relative_to(copies[0], output_html) if copies else "",
                marked_href_b=_relative_to(copies[1], output_html) if copies else "",
                coverage_a=coverage(sections, "a"),
                coverage_b=coverage(sections, "b"),
                heights_a={i + 1: doc_a[i].rect.height for i in range(doc_a.page_count)},
                heights_b={i + 1: doc_b[i].rect.height for i in range(doc_b.page_count)},
                # Embed the marked pages so a click can show the highlighted
                # source in the report itself — a PDF link at a viewer's mercy
                # is a fallback, not the feature. Resolution steps down with
                # length so a 60-page statement still ships as one file.
                previews_a=_previews(copies, 0, doc_a, doc_b, preview_dpi),
                previews_b=_previews(copies, 1, doc_a, doc_b, preview_dpi),
                ledger=led,
            )
        finally:
            if doc_a is not blank:
                doc_a.close()
            if doc_b is not blank:
                doc_b.close()
            blank.close()
        written = write_side_by_side(sections, meta, output_html)
        if console_html is not None:
            console = write_console(sections, meta, console_html)

    return SideBySideResult(
        pdf_a=pdf_a,
        pdf_b=pdf_b,
        sections=sections,
        pairs=pairs,
        summary=summary,
        output_html=written,
        console_html=console,
        marked_sections=shared_marks,
        marked_pdfs=list(copies) if copies else [],
        ledger=led,
    )
