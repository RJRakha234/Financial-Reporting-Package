"""secverify — validate an SEC-filing HTML against the published PDF.

Entirely offline: reads the two local files, writes a highlighted HTML review
copy (green = validated, amber = review, red = inconsistent + remark) and an
optional JSON report.  No network access of any kind.
"""

from __future__ import annotations

from pathlib import Path

from .annotate import Annotator, Issue, Result
from .pdfside import load_pdf

__all__ = ["verify", "Annotator", "Issue", "Result"]
__version__ = "1.0.0"


def verify(
    pdf_path: str | list[str],
    html_path: str,
    output_html: str | None = None,
) -> Result:
    """Compare *html_path* against one or several reference PDFs.

    ``pdf_path`` may be a single path or a list (e.g. the financial
    statements plus the signed auditor's report); the HTML is validated
    against their combined content.  ``output_html`` defaults to
    ``<html_path>.checked.html``; pass ``"none"`` to skip writing and just
    get the :class:`Result` back.
    """
    corpus = load_pdf(pdf_path)
    pdf_paths = [pdf_path] if isinstance(pdf_path, str) else list(pdf_path)
    html_text = Path(html_path).read_text(encoding="utf-8", errors="replace")
    result = Annotator(corpus).run(
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
