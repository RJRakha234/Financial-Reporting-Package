"""pdfhtmlcompare — verify a SEC HTML filing renders a published PDF correctly.

Compares the **financial tables** of a published statement PDF against the HTML
filed with the SEC, checking that the **numbers** and **wordings** render the
same and that no **line inside a table** is missing. Formatting differences are
ignored by design.

    from pdfhtmlcompare import compare
    result = compare("published.pdf", "filed.html",
                     output_pdf="validated.pdf", output_html="commented.html")
    print(result.consistent, result.validated_rows)
    for f in result.findings:
        print(f.page_label, f.kind, f.message())
"""

from .compare import ComparisonResult, Finding, RowResult, compare

__all__ = ["compare", "ComparisonResult", "Finding", "RowResult"]
