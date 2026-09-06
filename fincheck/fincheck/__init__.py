"""fincheck — check totals and subtotals in financial statements.

Public API::

    from fincheck import analyze
    result = analyze("statements.pdf", output_pdf="highlighted.pdf")
    print(result.consistent, result.issues)
"""

from dataclasses import dataclass, field

from .checks import Inconsistency, TotalCheck, run_checks
from .extract import extract_pages
from .highlight import write_highlighted_pdf
from .report import to_dict, to_json

__all__ = [
    "analyze",
    "AnalysisResult",
    "Inconsistency",
    "TotalCheck",
    "check_rollforward",
    "RollforwardResult",
    "RollforwardCheck",
]

# The rollforward names are imported lazily so that ``python -m
# fincheck.rollforward`` does not re-import a module already loaded by this
# package (which raises a RuntimeWarning).
_ROLLFORWARD_EXPORTS = {
    "check_rollforward",
    "RollforwardResult",
    "RollforwardCheck",
}


def __getattr__(name):
    if name in _ROLLFORWARD_EXPORTS:
        from . import rollforward

        return getattr(rollforward, name)
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
