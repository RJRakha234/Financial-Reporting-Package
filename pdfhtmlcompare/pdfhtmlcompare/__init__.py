"""pdfhtmlcompare — verify a SEC HTML filing renders a published PDF correctly.

Compares the **whole content** of a published statement PDF against the HTML
filed with the SEC — every word and number, not just the financial figures — and
reports whatever is changed, missing, or added, anchored to the PDF page.

    from pdfhtmlcompare import compare
    result = compare("published.pdf", "filed.html",
                     output_pdf="validated.pdf", output_html="commented.html")
    print(result.consistent, f"{result.coverage*100:.1f}% matched")
    for f in result.findings:
        print(f.page_label, f.kind, f.message())
"""

from .compare import ComparisonResult, Finding, compare

__all__ = ["compare", "ComparisonResult", "Finding"]
