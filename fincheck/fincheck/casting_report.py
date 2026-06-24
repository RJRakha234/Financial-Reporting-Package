"""Render casting results as console text, JSON and a colour-coded Excel workbook."""

from __future__ import annotations

import json

from .casting import CastCheck, CastResult
from .numbers import format_number

_STATUS_LABEL = {
    "ok": "OK",
    "mismatch": "MISMATCH",
    "unverified": "review",
    "not_additive": "n/a",
}


def _period(check: CastCheck) -> str:
    """A human label for what is being reconciled, e.g. 'H1 FY26 (Apr–Sep 2025)'."""
    return f"6M {check.year} = 3M {check.year} (current) + 3M {check.year} (prior)"


def _fmt(value: float | None) -> str:
    return "" if value is None else format_number(value)


# ---------------------------------------------------------------------------
# Dict / JSON
# ---------------------------------------------------------------------------

def to_dict(result: CastResult) -> dict:
    rows = []
    for c in result.checks:
        rows.append({
            "note": c.note,
            "table": c.title,
            "line_item": c.label.strip(),
            "year": c.year,
            "page": c.page_index + 1,
            "six_month": c.six_month,
            "current_quarter": c.current_quarter,
            "prior_quarter": c.prior_quarter,
            "expected": c.expected,
            "difference": c.difference,
            "basis": c.basis,
            "status": c.status(result.tolerance),
        })
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "current_pdf": result.current_pdf,
        "prior_pdf": result.prior_pdf,
        "tolerance": result.tolerance,
        "consistent": result.consistent,
        "counts": counts,
        "checks": rows,
        "uncast_tables": [
            {"note": t.note, "title": t.title, "page": t.page_index + 1,
             "rows": len(t.rows)}
            for t in result.uncast_tables
        ],
    }


def to_json(result: CastResult) -> str:
    return json.dumps(to_dict(result), indent=2)


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------

def to_console(result: CastResult) -> str:
    tol = result.tolerance
    ok = result.by_status("ok")
    mism = result.by_status("mismatch")
    unver = result.by_status("unverified")
    na = result.by_status("not_additive")

    lines: list[str] = []
    head = "✓ Every figure casts" if result.consistent else (
        f"✗ {len(mism)} figure(s) do not cast"
    )
    lines.append(head)
    lines.append("")
    lines.append(
        f"Cast {len(ok) + len(mism)} additive figures "
        f"(tolerance ±{format_number(tol)}): {len(ok)} OK, {len(mism)} mismatch"
        + (f", {len(unver)} could not be verified" if unver else "")
        + (f"; {len(na)} per-share/share-count figures not cast" if na else "")
        + "."
    )

    if mism:
        lines.append("")
        lines.append("Mismatches (year-to-date ≠ current 3M + prior 3M):")
        for n, c in enumerate(mism, 1):
            lines.append(
                f"  {n}. Note {c.note} · {c.label.strip()}  [{c.year}]"
            )
            lines.append(
                f"       6-month {_fmt(c.six_month):>14}   ({c.basis})"
            )
            lines.append(
                f"       3M(cur) {_fmt(c.current_quarter):>14}"
                f"  + 3M(prior) {_fmt(c.prior_quarter)}"
                f"  = {_fmt(c.expected)}   (off by {_fmt(c.difference)})"
            )

    if unver:
        lines.append("")
        lines.append("Could not be verified (a figure was missing in one PDF):")
        for c in unver:
            lines.append(f"  · Note {c.note} · {c.label.strip()} [{c.year}]")

    if result.uncast_tables:
        lines.append("")
        lines.append("Tables with no match in the prior statement (review manually):")
        for t in result.uncast_tables:
            lines.append(f"  · page {t.page_index + 1} · {t.note} {t.title}".rstrip())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def write_excel(result: CastResult, path: str) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    fills = {
        "ok": PatternFill("solid", fgColor="C6EFCE"),
        "mismatch": PatternFill("solid", fgColor="FFC7CE"),
        "unverified": PatternFill("solid", fgColor="FFEB9C"),
        "not_additive": PatternFill("solid", fgColor="E7E6E6"),
    }
    status_font = {
        "ok": Font(color="006100"),
        "mismatch": Font(color="9C0006", bold=True),
        "unverified": Font(color="9C6500"),
        "not_additive": Font(color="808080", italic=True),
    }
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    num_fmt = "#,##0;(#,##0)"

    wb = Workbook()

    # --- Summary sheet ----------------------------------------------------
    summary = wb.active
    summary.title = "Summary"
    counts: dict[str, int] = {}
    for c in result.checks:
        s = c.status(result.tolerance)
        counts[s] = counts.get(s, 0) + 1

    summary["A1"] = "Casting report"
    summary["A1"].font = Font(size=16, bold=True)
    meta = [
        ("Current-period statement", result.current_pdf),
        ("Prior-period statement", result.prior_pdf),
        ("Tolerance (± units)", format_number(result.tolerance)),
        ("", ""),
        ("Figures cast (additive)", counts.get("ok", 0) + counts.get("mismatch", 0)),
        ("  Casts correctly (OK)", counts.get("ok", 0)),
        ("  Does NOT cast (mismatch)", counts.get("mismatch", 0)),
        ("  Could not be verified", counts.get("unverified", 0)),
        ("Per-share / share-count (not cast)", counts.get("not_additive", 0)),
    ]
    for i, (k, v) in enumerate(meta, start=3):
        summary[f"A{i}"] = k
        summary[f"B{i}"] = v
        summary[f"A{i}"].font = Font(bold=k.strip() != "" and not k.startswith("  "))
    summary.column_dimensions["A"].width = 36
    summary.column_dimensions["B"].width = 70

    note = (
        "Casting identity: the year-to-date (six-month) figure in the current "
        "statement must equal the current quarter (three-month) figure plus the "
        "same line item's three-month figure from the prior statement — checked "
        "for both the current and the comparative year. Differences within the "
        "tolerance (rounding drift) are treated as OK; the exact difference is "
        "shown on the Casting sheet either way."
    )
    summary[f"A{len(meta) + 4}"] = note
    summary[f"A{len(meta) + 4}"].alignment = Alignment(wrap_text=True, vertical="top")
    summary.merge_cells(
        start_row=len(meta) + 4, start_column=1,
        end_row=len(meta) + 8, end_column=2,
    )

    # --- Casting sheet ----------------------------------------------------
    ws = wb.create_sheet("Casting")
    headers = [
        "Note", "Statement / table", "Line item", "Year", "Page",
        "Year-to-date (6M)", "Current qtr (3M)", "Prior qtr (3M)",
        "Expected", "Difference", "Basis", "Status",
    ]
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = border

    for c in result.checks:
        status = c.status(result.tolerance)
        ws.append([
            c.note, c.title, c.label.strip(), c.year, c.page_index + 1,
            c.six_month, c.current_quarter, c.prior_quarter,
            c.expected, c.difference, c.basis, _STATUS_LABEL.get(status, status),
        ])
        r = ws.max_row
        for col in range(1, len(headers) + 1):
            cell = ws.cell(row=r, column=col)
            cell.border = border
            if col in (6, 7, 8, 9, 10):
                cell.number_format = num_fmt
        fill = fills.get(status)
        if fill:
            for col in (10, 12):
                ws.cell(row=r, column=col).fill = fill
        f = status_font.get(status)
        if f:
            ws.cell(row=r, column=12).font = f

    widths = [8, 30, 46, 7, 6, 17, 16, 15, 14, 11, 22, 11]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"

    wb.save(path)
    return path
