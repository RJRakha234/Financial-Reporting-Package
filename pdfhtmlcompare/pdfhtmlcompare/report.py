"""Console and JSON rendering of a comparison result."""

import json

from .compare import (
    FIGURE_CHANGED,
    FIGURE_EXTRA,
    FIGURE_MISSING,
    LINE_EXTRA,
    LINE_MISSING,
    WORD_CHANGED,
    ComparisonResult,
)

_LABEL = {
    FIGURE_CHANGED: "number changed",
    FIGURE_MISSING: "number missing from HTML",
    FIGURE_EXTRA: "number only in HTML",
    WORD_CHANGED: "wording differs",
    LINE_MISSING: "table line missing from HTML",
    LINE_EXTRA: "table line only in HTML",
}


def to_dict(result: ComparisonResult) -> dict:
    return {
        "source_pdf": result.source_pdf,
        "source_html": result.source_html,
        "consistent": result.consistent,
        "validated_rows": result.validated_rows,
        "finding_count": len(result.findings),
        "numbers_changed": len(result.by_kind(FIGURE_CHANGED)),
        "numbers_missing": len(result.by_kind(FIGURE_MISSING)),
        "numbers_only_in_html": len(result.by_kind(FIGURE_EXTRA)),
        "wording_differs": len(result.by_kind(WORD_CHANGED)),
        "lines_missing_from_html": len(result.by_kind(LINE_MISSING)),
        "lines_only_in_html": len(result.by_kind(LINE_EXTRA)),
        "pdf_figures": result.pdf_figures,
        "matched_figures": result.matched_figures,
        "findings": [
            {
                "kind": f.kind,
                "page": (f.page + 1) if f.page is not None else None,
                "label": f.label.strip(),
                "pdf": f.pdf_text,
                "html": f.html_text,
                "message": f.message(),
            }
            for f in result.findings
        ],
    }


def to_json(result: ComparisonResult) -> str:
    return json.dumps(to_dict(result), indent=2)


def to_console(result: ComparisonResult) -> str:
    if not result.findings:
        return (
            "✓ Financial tables match: "
            f"{result.validated_rows} rows validated, "
            f"{result.matched_figures}/{result.pdf_figures} figures aligned, "
            "no missing table lines."
        )
    lines = [f"✗ Found {len(result.findings)} finding(s) in the financial tables:", ""]
    shown = result.findings[:80]
    for n, f in enumerate(shown, 1):
        lines.append(f"  {n}. {f.page_label}  ·  [{_LABEL[f.kind]}]")
        lines.append(f"     {f.message()}")
        lines.append("")
    if len(result.findings) > len(shown):
        lines.append(f"  … and {len(result.findings) - len(shown)} more (see --json).")
        lines.append("")
    lines.append(
        f"Rows validated: {result.validated_rows}. "
        f"Figures matched: {result.matched_figures} of {result.pdf_figures}."
    )
    return "\n".join(lines)
