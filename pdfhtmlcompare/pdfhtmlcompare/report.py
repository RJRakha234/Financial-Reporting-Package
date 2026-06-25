"""Console and JSON rendering of a whole-content comparison result."""

import json

from .compare import ADDED, CHANGED, MISSING, ComparisonResult

_LABEL = {CHANGED: "changed", MISSING: "missing from HTML", ADDED: "only in HTML"}


def to_dict(result: ComparisonResult) -> dict:
    return {
        "source_pdf": result.source_pdf,
        "source_html": result.source_html,
        "consistent": result.consistent,
        "pdf_tokens": result.pdf_tokens,
        "matched_tokens": result.matched_tokens,
        "coverage_pct": round(result.coverage * 100, 2),
        "finding_count": len(result.findings),
        "changed": len(result.by_kind(CHANGED)),
        "missing_from_html": len(result.by_kind(MISSING)),
        "only_in_html": len(result.by_kind(ADDED)),
        "findings": [
            {
                "kind": f.kind,
                "page": (f.page + 1) if f.page is not None else None,
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
    head = (
        f"Coverage: {result.matched_tokens}/{result.pdf_tokens} PDF tokens "
        f"({result.coverage * 100:.1f}%) matched the HTML."
    )
    if not result.findings:
        return "✓ Whole PDF content matches the HTML.\n" + head
    lines = [f"✗ Found {len(result.findings)} difference(s):", ""]
    shown = result.findings[:100]
    for n, f in enumerate(shown, 1):
        lines.append(f"  {n}. {f.page_label}  ·  [{_LABEL[f.kind]}]")
        lines.append(f"     {f.message()}")
        lines.append("")
    if len(result.findings) > len(shown):
        lines.append(f"  … and {len(result.findings) - len(shown)} more (see --json).")
        lines.append("")
    lines.append(head)
    return "\n".join(lines)
