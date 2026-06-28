"""fincheck — check totals and subtotals in financial statements.

Public API::

    from fincheck import analyze
    result = analyze("statements.pdf", output_pdf="highlighted.pdf")
    print(result.consistent, result.issues)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # for type checkers only — no runtime import of the PDF stack.
    from .checks import Inconsistency, TotalCheck

__all__ = [
    "analyze",
    "AnalysisResult",
    "Inconsistency",
    "TotalCheck",
    "convert_workbook",
]


def __getattr__(name: str):
    """Lazily expose names so importing a submodule (e.g. ``fincheck.xlsx2csv``)
    does not drag in the PDF dependencies until they are actually needed."""
    if name in ("Inconsistency", "TotalCheck"):
        from . import checks

        return getattr(checks, name)
    if name == "convert_workbook":
        from .xlsx2csv import convert_workbook

        return convert_workbook
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        from .report import to_dict

        return to_dict(self.issues)

    def as_json(self) -> str:
        from .report import to_json

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
    from .checks import run_checks
    from .extract import extract_pages
    from .highlight import write_highlighted_pdf

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
