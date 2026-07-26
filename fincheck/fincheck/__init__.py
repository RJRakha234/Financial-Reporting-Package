"""fincheck — check totals in a financial statement, or compare two PDFs exactly.

Public API::

    from fincheck import analyze, compare
    result = analyze("statements.pdf", output_pdf="highlighted.pdf")
    print(result.consistent, result.issues)

    diff = compare("draft.pdf", "final.pdf", output_pdf="diff.pdf")
    print(diff.identical, diff.numeric_changes)
"""

from dataclasses import dataclass, field

from .align import align, group, summarise, units_of
from .blocks import segment
from .checks import Inconsistency, TotalCheck, run_checks
from .compare import DEFAULT_DPI, ComparisonResult, SpanChange, compare_pdfs
from .diffmark import write_diff_pdf
from .extract import extract_pages
from .highlight import write_highlighted_pdf
from .report import to_dict, to_json
from .sidebyside import Meta, write_side_by_side

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


def side_by_side(
    pdf_a: str,
    pdf_b: str,
    output_html: str | None = None,
) -> SideBySideResult:
    """Match two documents paragraph by paragraph and row by row.

    Unlike :func:`compare`, this reconstructs paragraphs and tables from the
    page geometry and pairs them by similarity, so its output is a reviewer's
    worksheet rather than proof. Use :func:`compare` when you need certainty.

    Args:
        pdf_a: the reference PDF, shown on the left.
        pdf_b: the PDF compared against it, shown on the right.
        output_html: if given, write the side-by-side page here.
    """
    units_a = units_of(segment(pdf_a))
    units_b = units_of(segment(pdf_b))
    pairs = align(units_a, units_b)
    sections = group(pairs)
    summary = summarise(pairs, units_a, units_b)

    written = None
    if output_html is not None:
        import fitz

        doc_a, doc_b = fitz.open(pdf_a), fitz.open(pdf_b)
        try:
            meta = Meta(
                pdf_a=pdf_a,
                pdf_b=pdf_b,
                pages_a=doc_a.page_count,
                pages_b=doc_b.page_count,
                summary=summary,
            )
        finally:
            doc_a.close()
            doc_b.close()
        written = write_side_by_side(sections, meta, output_html)

    return SideBySideResult(
        pdf_a=pdf_a,
        pdf_b=pdf_b,
        sections=sections,
        pairs=pairs,
        summary=summary,
        output_html=written,
    )
