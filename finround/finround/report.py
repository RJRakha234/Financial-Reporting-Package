"""Human- and machine-readable accounts of what the rounding did."""

from __future__ import annotations

import json
from fractions import Fraction


def render_report(result, show_table: bool = True) -> str:
    table, spec = result.table, result.spec
    lines: list[str] = []

    if show_table:
        marks = set(result.adjusted_cells())
        lines.append(table.render(result.formatted(), marks=marks))
        lines.append("")

    lines.append(f"Rounded to {spec.describe()}.")

    rows, cols = len(result.row_groups), len(result.col_groups)
    structure = []
    if rows:
        structure.append(f"{rows} row total{'s' if rows != 1 else ''}")
    if cols:
        structure.append(f"{cols} column total{'s' if cols != 1 else ''}")
    lines.append(
        "Structure: " + (" and ".join(structure) if structure else "no totals found")
        + f" over {table.n_rows}x{table.n_cols} figures."
    )

    problems = result.violations()
    constraints = rows * table.n_cols + cols * table.n_rows
    if problems:
        lines.append(f"✗ {len(problems)} constraint(s) still do not foot:")
        lines.extend(f"    {p}" for p in problems)
    elif constraints:
        checks = "check" if constraints == 1 else "checks"
        lines.append(f"✓ all {constraints} total {checks} foot exactly after rounding.")

    adjusted = result.adjusted_cells()
    if adjusted:
        moved = ", ".join(
            f"{table.row_labels[i]}/{table.col_labels[j]} "
            f"({'up' if result.units[i][j] > result.naive_units[i][j] else 'down'})"
            for i, j in adjusted[:8]
        )
        more = f", and {len(adjusted) - 8} more" if len(adjusted) > 8 else ""
        lines.append(
            f"{len(adjusted)} of {table.n_rows * table.n_cols} figures were moved off "
            f"their nearest value (marked *) so the table foots: {moved}{more}."
        )
    else:
        lines.append(
            "Every figure is at its nearest value — the table already foots on "
            "a straight rounding."
        )

    worst = result.max_deviation()
    lines.append(
        f"Cost of consistency: total movement {_fmt(result.deviation())} steps "
        f"against {_fmt(result.deviation(result.naive_units))} for straight rounding; "
        f"no figure moved more than {_fmt(worst)} of a step."
    )

    for warning in result.warnings:
        lines.append(f"! {warning}")
    return "\n".join(lines)


def to_dict(result) -> dict:
    table = result.table
    return {
        "step": str(result.spec.step),
        "scale": str(result.spec.scale),
        "row_labels": table.row_labels,
        "col_labels": table.col_labels,
        "values": [[float(v) for v in row] for row in result.values],
        "formatted": result.formatted(),
        "row_groups": {str(k): v for k, v in sorted(result.row_groups.items())},
        "col_groups": {str(k): v for k, v in sorted(result.col_groups.items())},
        "consistent": result.consistent,
        "violations": result.violations(),
        "adjusted_cells": [list(cell) for cell in result.adjusted_cells()],
        "total_movement_steps": float(result.deviation()),
        "naive_movement_steps": float(result.deviation(result.naive_units)),
        "max_cell_movement_steps": float(result.max_deviation()),
        "extra_slack_steps": result.slack_used,
        "warnings": result.warnings,
    }


def to_json(result) -> str:
    return json.dumps(to_dict(result), indent=2)


def _fmt(value: Fraction) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".") or "0"
