"""Console and JSON reporting."""

from __future__ import annotations

import json

from .annotate import Result


def to_console(result: Result) -> str:
    lines = []
    if result.issues:
        lines.append(f"✗ Found {len(result.issues)} inconsistencies:\n")
        for issue in result.issues:
            lines.append(
                f"  #{issue.num} [{issue.kind}/{issue.severity}] {issue.excerpt}"
            )
            lines.append(f"      remark: {issue.remark[:300]}")
    else:
        lines.append("✓ No inconsistencies — HTML fully validated against the PDF.")
    lines.append(
        f"\nFigures: {result.figures_ok}/{result.figures_total} validated"
        f" ({result.figures_bad} not found in PDF)"
    )
    lines.append(
        f"Text blocks: {result.text_blocks_ok}/{result.text_blocks_total} matched, "
        f"{result.text_blocks_review} to review, {result.text_blocks_bad} not found"
    )
    cov = result.coverage
    lines.append(
        f"PDF → HTML coverage: {cov.ok}/{cov.total} PDF lines reflected, "
        f"{cov.review} to review, {cov.missing} missing"
    )
    if result.pdf_figures_missing:
        lines.append(
            f"⚠ {len(result.pdf_figures_missing)} significant PDF figures never "
            "appear in the HTML (see report / summary panel)."
        )
    return "\n".join(lines)


def to_json(result: Result) -> str:
    return json.dumps(
        {
            "figures": {
                "total": result.figures_total,
                "validated": result.figures_ok,
                "not_found": result.figures_bad,
            },
            "text_blocks": {
                "total": result.text_blocks_total,
                "matched": result.text_blocks_ok,
                "review": result.text_blocks_review,
                "not_found": result.text_blocks_bad,
            },
            "figure_count_mismatches": result.figure_count_mismatches,
            "pdf_coverage": {
                "lines_total": result.coverage.total,
                "reflected": result.coverage.ok,
                "review": result.coverage.review,
                "missing": result.coverage.missing,
                "flagged_lines": [
                    {
                        "pdf_page": line.label or line.page,
                        "pdf_text": line.text,
                        "status": line.status,
                        "remark": line.remark,
                    }
                    for line in result.coverage.lines
                    if line.status != "ok"
                ],
            },
            "issues": [
                {
                    "num": i.num,
                    "kind": i.kind,
                    "severity": i.severity,
                    "tier": i.tier,
                    "html_content": i.excerpt,
                    "remark": i.remark,
                    "anchor": i.anchor,
                }
                for i in result.issues
            ],
            "pdf_figures_missing_from_html": result.pdf_figures_missing,
        },
        indent=2,
        ensure_ascii=False,
    )
