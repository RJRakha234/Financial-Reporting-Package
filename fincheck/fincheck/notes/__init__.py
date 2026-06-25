"""Cross-statement common-notes comparison.

Compare the common notes shared across the four quarter-end financial
statements (Consolidated/Standalone Ind AS, IFRS INR/USD), neutralizing the
*expected* differences (entity, framework, currency) and surfacing the
substantive wording differences for a checker to accept or ignore.

Public API::

    from fincheck.notes import load_document, compare_documents, write_html_report

    docs = [load_document("standalone.pdf"), load_document("consol.pdf")]
    result = compare_documents(docs)
    write_html_report(result, "notes_diff.html")
"""

from .compare import (
    ComparisonResult,
    Difference,
    Document,
    PairComparison,
    compare_documents,
    load_document,
)
from .ledger import Ledger, load_ledger, save_ledger
from .report import render_html, write_html_report

__all__ = [
    "load_document",
    "compare_documents",
    "Document",
    "ComparisonResult",
    "PairComparison",
    "Difference",
    "Ledger",
    "load_ledger",
    "save_ledger",
    "render_html",
    "write_html_report",
]
