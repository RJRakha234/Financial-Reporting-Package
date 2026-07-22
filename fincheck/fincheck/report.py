"""Render the list of inconsistencies as console text or JSON."""

import json

from .checks import Inconsistency
from .numbers import format_number


def to_dict(issues: list[Inconsistency]) -> dict:
    return {
        "inconsistency_count": len(issues),
        "consistent": not issues,
        "inconsistencies": [
            {
                "page": i.page_index + 1,
                "column": i.column,
                "label": i.label.strip(),
                "stated": i.stated,
                "expected": i.expected,
                "difference": i.difference,
                "components": [
                    {"label": name.strip(), "value": value}
                    for name, value in i.components
                ],
            }
            for i in issues
        ],
    }


def to_json(issues: list[Inconsistency]) -> str:
    return json.dumps(to_dict(issues), indent=2)


def to_console(issues: list[Inconsistency]) -> str:
    if not issues:
        return "✓ All totals and subtotals foot correctly."

    lines = [
        f"✗ Found {len(issues)} total/subtotal inconsistenc"
        f"{'y' if len(issues) == 1 else 'ies'}:",
        "",
    ]
    for n, i in enumerate(issues, 1):
        lines.append(f"  {n}. Page {i.page_index + 1}  ·  {i.label.strip()}")
        lines.append(
            f"     stated   {format_number(i.stated):>16}"
        )
        lines.append(
            f"     expected {format_number(i.expected):>16}"
            f"   (off by {format_number(i.difference)})"
        )
        comp = "  +  ".join(
            f"{name.strip()} {format_number(v)}" for name, v in i.components
        )
        if comp:
            lines.append(f"     = {comp}")
        lines.append("")
    return "\n".join(lines)


# --- Rendering for document comparison (compare.py) -------------------------
# These take a ComparisonResult but read only its attributes, so report.py does
# not import compare.py (which would create an import cycle).

def _snippet(text: str, limit: int = 80) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def comparison_to_dict(result) -> dict:
    return {
        "reference": result.reference_path,
        "scanned": result.scanned_path,
        "reference_pages": result.reference_pages,
        "scanned_pages": result.scanned_pages,
        "identical": result.identical,
        "similarity": round(result.similarity, 4),
        "ocr_used": result.ocr_used,
        "number_change_count": len(result.number_changes),
        "number_changes": [
            {
                "kind": c.kind,
                "reference": c.reference,
                "scanned": c.scanned,
            }
            for c in result.number_changes
        ],
        "text_difference_count": len(result.segments),
        "text_differences": [
            {"tag": s.tag, "reference": s.reference, "scanned": s.scanned}
            for s in result.segments
        ],
    }


def comparison_to_json(result) -> str:
    return json.dumps(comparison_to_dict(result), indent=2)


def comparison_to_console(result) -> str:
    pct = f"{result.similarity * 100:.1f}%"
    header = (
        f"reference: {result.reference_path} ({result.reference_pages} page"
        f"{'s' if result.reference_pages != 1 else ''})\n"
        f"scanned:   {result.scanned_path} ({result.scanned_pages} page"
        f"{'s' if result.scanned_pages != 1 else ''})"
        + ("   [OCR used]" if result.ocr_used else "")
    )

    if result.identical:
        return (
            f"✓ The scanned document matches the reference PDF "
            f"(word similarity {pct}).\n\n{header}"
        )

    lines = [f"✗ Documents differ (word similarity {pct}).", "", header, ""]

    numbers = result.number_changes
    if numbers:
        lines.append(
            f"Figures that do not match ({len(numbers)}):"
        )
        for n, c in enumerate(numbers, 1):
            if c.kind == "changed":
                lines.append(
                    f"  {n}. reference {format_number(c.reference)}  ->  "
                    f"scanned {format_number(c.scanned)}"
                )
            elif c.kind == "missing_in_scan":
                lines.append(
                    f"  {n}. {format_number(c.reference)} "
                    "— in reference, not found in scan"
                )
            else:  # extra_in_scan
                lines.append(
                    f"  {n}. {format_number(c.scanned)} "
                    "— in scan, not in reference"
                )
        lines.append("")

    segments = result.segments
    if segments:
        shown = segments[:20]
        lines.append(f"Text differences ({len(segments)}):")
        for n, s in enumerate(shown, 1):
            if s.tag == "replace":
                lines.append(
                    f"  {n}. changed: '{_snippet(s.reference)}'  ->  "
                    f"'{_snippet(s.scanned)}'"
                )
            elif s.tag == "delete":
                lines.append(
                    f"  {n}. only in reference: '{_snippet(s.reference)}'"
                )
            else:  # insert
                lines.append(f"  {n}. only in scan: '{_snippet(s.scanned)}'")
        if len(segments) > len(shown):
            lines.append(f"  … and {len(segments) - len(shown)} more.")
        lines.append("")

    return "\n".join(lines).rstrip()
