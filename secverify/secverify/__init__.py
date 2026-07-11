"""secverify — validate an SEC-filing HTML against the published PDF.

Entirely offline: reads the two local files, writes a highlighted HTML review
copy (green = validated, amber = review, red = inconsistent + remark) and an
optional JSON report.  No network access of any kind.
"""

from __future__ import annotations

from pathlib import Path

from .annotate import Annotator, Issue, Result
from .pdfside import load_pdf

__all__ = ["verify", "Annotator", "Issue", "Result", "LEVELS"]
__version__ = "1.0.0"

#: cumulative capability levels — each includes all lower levels
#:   base   — the stable text/figure engine
#:   alpha  — + Phase 1: date-header check, identifier-association check
#:   beta   — + Phase 2: geometry-based table-grid cell comparison
#:   sigma  — + Phase 3: render-diff, OCR of scanned pages, hidden-text
LEVELS = {"base": 0, "alpha": 1, "beta": 2, "sigma": 3}


def verify(
    pdf_path: str | list[str],
    html_path: str,
    output_html: str | None = None,
    level: str = "base",
    review_zones: bool = False,
    strict: bool = False,
    footed: bool = False,
) -> Result:
    """Compare *html_path* against one or several reference PDFs.

    ``pdf_path`` may be a single path or a list (e.g. the financial
    statements plus the signed auditor's report); the HTML is validated
    against their combined content.  ``output_html`` defaults to
    ``<html_path>.checked.html``; pass ``"none"`` to skip writing and just
    get the :class:`Result` back.  ``level`` selects the capability tier
    (see :data:`LEVELS`).
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}; choose from {list(LEVELS)}")
    corpus = load_pdf(pdf_path)
    pdf_paths = [pdf_path] if isinstance(pdf_path, str) else list(pdf_path)
    html_text = Path(html_path).read_text(encoding="utf-8", errors="replace")
    result = Annotator(
        corpus, level=level, pdf_paths=pdf_paths,
        review_zones=review_zones, strict=strict, footed=footed,
    ).run(
        html_text,
        " + ".join(Path(p).name for p in pdf_paths),
        Path(html_path).name,
    )
    if output_html is None:
        output_html = str(Path(html_path).with_suffix("")) + ".checked.html"
    if output_html.lower() != "none":
        Path(output_html).write_text(result.html_out, encoding="utf-8")
        result.output_html = output_html  # type: ignore[attr-defined]
    else:
        result.output_html = None  # type: ignore[attr-defined]
    return result
