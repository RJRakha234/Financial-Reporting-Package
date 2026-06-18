"""finmatch — compare two financial-statement PDFs by *language*, ignoring the
numbers, and highlight what matches and what does not.

Typical use: confirm that two currency versions of the same IFRS statement
(e.g. INR and USD) carry identical wording, line items and notes, so the only
differences are the figures themselves.
"""

from pathlib import Path

from .compare import Result, compare_lines
from .extract import extract_lines
from .highlight import highlight

__all__ = ["analyze", "Result", "compare_lines", "extract_lines"]


def analyze(
    pdf_a: str,
    pdf_b: str,
    output_a: str | None = None,
    output_b: str | None = None,
    mask_currency: bool = True,
    ignore_case: bool = True,
) -> Result:
    """Compare two PDFs and (optionally) write highlighted copies.

    ``output_a`` / ``output_b`` default to ``<input>.compared.pdf``; pass
    ``"none"`` to skip writing a highlighted PDF.
    """
    lines_a = extract_lines(pdf_a)
    lines_b = extract_lines(pdf_b)
    result = compare_lines(
        lines_a, lines_b, mask_currency=mask_currency, ignore_case=ignore_case
    )

    def _default(path, override):
        if override == "none":
            return None
        if override:
            return override
        p = Path(path)
        return str(p.with_suffix(".compared.pdf"))

    out_a = _default(pdf_a, output_a)
    out_b = _default(pdf_b, output_b)
    if out_a and out_b:
        highlight(result, pdf_a, pdf_b, out_a, out_b)
    result.output_a = out_a  # type: ignore[attr-defined]
    result.output_b = out_b  # type: ignore[attr-defined]
    return result
