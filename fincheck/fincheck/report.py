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
